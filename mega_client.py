# ============================================================
#  mega_client.py  —  Lightweight MEGA API client
#  Replaces the broken mega.py library (incompatible with Python 3.12)
#  Uses only: requests, pycryptodome (already in requirements)
# ============================================================

import base64
import hashlib
import json
import os
import random
import struct

import requests
from Crypto.Cipher import AES
from Crypto.Util import Counter


MEGA_API = "https://g.api.mega.co.nz/cs"


# ── Low-level crypto helpers ──────────────────────────────────

def _a32_to_bytes(a):
    return struct.pack(">%dI" % len(a), *a)


def _bytes_to_a32(b):
    # Pad to multiple of 4
    pad = (4 - len(b) % 4) % 4
    b += b"\x00" * pad
    return struct.unpack(">%dI" % (len(b) // 4), b)


def _base64_url_decode(s):
    s = s.replace("-", "+").replace("_", "/")
    pad = 4 - len(s) % 4
    if pad != 4:
        s += "=" * pad
    return base64.b64decode(s)


def _base64_url_encode(b):
    return base64.b64encode(b).decode().replace("+", "-").replace("/", "_").rstrip("=")


def _aes_cbc_decrypt(key_a32, data):
    key_bytes = _a32_to_bytes(key_a32)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=b"\x00" * 16)
    return cipher.decrypt(data)


def _aes_cbc_encrypt(key_a32, data):
    key_bytes = _a32_to_bytes(key_a32)
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv=b"\x00" * 16)
    return cipher.encrypt(data)


def _aes_ctr_crypt(key_a32, iv_a32, data):
    key_bytes = _a32_to_bytes(key_a32)
    iv_int = (iv_a32[0] << 96) | (iv_a32[1] << 64) | (iv_a32[2] << 32) | iv_a32[3]
    ctr = Counter.new(128, initial_value=iv_int)
    cipher = AES.new(key_bytes, AES.MODE_CTR, counter=ctr)
    return cipher.encrypt(data)


def _derive_key(password: str) -> list:
    """Derive AES key from MEGA password."""
    pw_bytes = password.encode("utf-8")
    key = [0x93C467E3, 0x7DB0C7A4, 0xD1BE3F81, 0x0152CB56]
    p = [int.from_bytes(pw_bytes[i:i+4].ljust(4, b"\x00"), "big")
         for i in range(0, len(pw_bytes), 4)]
    if not p:
        p = [0]
    for _ in range(65536):
        for j in range(0, len(p), 4):
            block = p[j:j+4]
            while len(block) < 4:
                block.append(0)
            key = list(_bytes_to_a32(_aes_cbc_encrypt(key, _a32_to_bytes(block))))
    return key


def _hash_email(email: str, key: list) -> str:
    """Hash email for MEGA login."""
    h = [0, 0, 0, 0]
    email_bytes = email.lower().encode("utf-8")
    e = [int.from_bytes(email_bytes[i:i+4].ljust(4, b"\x00"), "big")
         for i in range(0, len(email_bytes), 4)]
    for i, v in enumerate(e):
        h[i % 4] ^= v
    hashed = h[:]
    for _ in range(16384):
        hashed = list(_bytes_to_a32(_aes_cbc_encrypt(key, _a32_to_bytes(hashed))))
    result = _a32_to_bytes([hashed[0], hashed[2]])
    return _base64_url_encode(result)


# ── API request helper ────────────────────────────────────────

_seq = random.randint(0, 0xFFFFFFFF)


def _api_request(data, sid=None):
    global _seq
    _seq += 1
    params = {"id": _seq}
    if sid:
        params["sid"] = sid

    resp = requests.post(
        MEGA_API,
        params=params,
        data=json.dumps([data]),
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()

    if isinstance(result, list):
        result = result[0]

    if isinstance(result, int) and result < 0:
        errors = {
            -1: "EINTERNAL",
            -2: "EARGS",
            -3: "EAGAIN",
            -4: "ERATELIMIT",
            -6: "ENOENT (wrong email or password)",
            -9: "ENOENT",
            -11: "EACCESS",
            -12: "EEXIST",
            -14: "EKEY",
            -15: "ESID (session expired)",
            -16: "EBLOCKED",
            -17: "EOVERQUOTA",
            -18: "ETEMPUNAVAIL",
        }
        raise ConnectionError(f"MEGA API error: {errors.get(result, str(result))}")

    return result


# ── Public MegaClient class ───────────────────────────────────

class MegaClient:
    """Minimal MEGA client supporting login, get_files, rename, get_user."""

    def __init__(self):
        self._sid = None
        self._master_key = None
        self._uid = None

    # ── Login ─────────────────────────────────────────────────

    def login(self, email: str, password: str) -> "MegaClient":
        """Login with email+password. Returns self on success, raises on failure."""
        email = email.strip().lower()
        password = password.strip()

        pw_key = _derive_key(password)
        hashed = _hash_email(email, pw_key)

        # Step 1: get user salt / login info
        resp = _api_request({"a": "us", "user": email, "uh": hashed})

        if isinstance(resp, int):
            raise ConnectionError(f"MEGA login failed (code {resp}). Check email/password.")

        if "tsid" in resp:
            # Old-style session (no encryption)
            tsid = _base64_url_decode(resp["tsid"])
            key = _a32_to_bytes(pw_key)
            cipher = AES.new(key, AES.MODE_CBC, iv=b"\x00" * 16)
            sid_check = cipher.encrypt(tsid[:16])
            if sid_check[:8] != tsid[16:24]:
                raise ConnectionError("MEGA login failed: wrong email or password.")
            self._sid = resp["tsid"]
        elif "csid" in resp:
            enc_master_key = _base64_url_decode(resp["k"])
            enc_sid = _base64_url_decode(resp["csid"])

            # Decrypt master key with pw_key
            master_key_bytes = _aes_cbc_decrypt(pw_key, enc_master_key)
            self._master_key = list(_bytes_to_a32(master_key_bytes))

            # Decode RSA private key
            privk_bytes = _aes_cbc_decrypt(
                self._master_key, _base64_url_decode(resp["privk"])
            )
            # Parse RSA key components
            privk = list(_bytes_to_a32(privk_bytes))
            rsa_key = self._parse_rsa_key(privk)

            # Decrypt session ID with RSA key
            sid_int = self._rsa_decrypt(
                int.from_bytes(enc_sid, "big"), rsa_key
            )
            sid_bytes = sid_int.to_bytes(43, "big")
            self._sid = _base64_url_encode(sid_bytes[:43])
        else:
            raise ConnectionError("MEGA login failed: unexpected response format.")

        return self

    def _parse_rsa_key(self, privk_a32):
        """Parse RSA private key from a32 array."""
        pos = 0
        components = []
        raw = _a32_to_bytes(privk_a32)
        idx = 0
        for _ in range(4):
            # Each component is length-prefixed (2 bytes, big-endian bit length)
            bit_len = (raw[idx] << 8) | raw[idx + 1]
            idx += 2
            byte_len = (bit_len + 7) // 8
            comp = int.from_bytes(raw[idx:idx + byte_len], "big")
            components.append(comp)
            idx += byte_len
        return components  # [p, q, d, u] or [p, q, d, ...] — we only need first 3

    def _rsa_decrypt(self, ciphertext: int, key: list) -> int:
        """RSA decrypt: m = c^d mod (p*q)."""
        p, q, d = key[0], key[1], key[2]
        n = p * q
        return pow(ciphertext, d, n)

    # ── Account info (used to verify login) ───────────────────

    def get_user(self) -> dict:
        """Fetch account info — throws if session is invalid."""
        if not self._sid:
            raise ConnectionError("Not logged in.")
        return _api_request({"a": "ug"}, sid=self._sid)

    # ── File listing ──────────────────────────────────────────

    def get_files(self) -> dict:
        """Return dict of {file_id: file_data} for all files in the account."""
        if not self._sid:
            raise ConnectionError("Not logged in.")
        resp = _api_request({"a": "f", "c": 1}, sid=self._sid)
        files = {}
        for f in resp.get("f", []):
            fid = f.get("h")
            if not fid:
                continue
            # Decrypt attributes if we have a key
            a = self._decrypt_attrs(f)
            files[fid] = {**f, "a": a}
        return files

    def _decrypt_attrs(self, f: dict) -> dict:
        """Decrypt file attributes (name etc.) — returns dict or {} on failure."""
        try:
            raw_key = f.get("k", "")
            if ":" in raw_key:
                raw_key = raw_key.split(":")[1]
            key_bytes = _base64_url_decode(raw_key)
            key_a32 = list(_bytes_to_a32(key_bytes))

            # File key decryption
            if len(key_a32) == 4:
                file_key = list(_bytes_to_a32(
                    _aes_cbc_decrypt(self._master_key, _a32_to_bytes(key_a32))
                ))
            elif len(key_a32) == 8:
                # Folder key
                file_key = [
                    key_a32[0] ^ key_a32[4],
                    key_a32[1] ^ key_a32[5],
                    key_a32[2] ^ key_a32[6],
                    key_a32[3] ^ key_a32[7],
                ]
                file_key = list(_bytes_to_a32(
                    _aes_cbc_decrypt(self._master_key, _a32_to_bytes(file_key))
                ))
            else:
                return {}

            attr_bytes = _base64_url_decode(f.get("a", ""))
            # Decrypt attrs with file_key
            dec = _aes_cbc_decrypt(file_key[:4], attr_bytes)
            # Strip MEGA: prefix and null padding
            dec = dec.rstrip(b"\x00")
            prefix = b"MEGA{"
            if dec.startswith(prefix):
                json_str = dec[5:]  # skip "MEGA{"
                # Find closing }
                attrs = json.loads("{" + json_str.decode("utf-8", errors="replace"))
                return attrs
        except Exception:
            pass
        return {}

    # ── Rename ────────────────────────────────────────────────

    def rename(self, file_data: dict, new_name: str):
        """Rename a file. file_data is one entry from get_files()."""
        if not self._sid:
            raise ConnectionError("Not logged in.")

        fid = file_data.get("h")
        if not fid:
            raise ValueError("Invalid file data: missing handle.")

        # Rebuild file key for attribute encryption
        raw_key = file_data.get("k", "")
        if ":" in raw_key:
            raw_key = raw_key.split(":")[1]
        key_bytes = _base64_url_decode(raw_key)
        key_a32 = list(_bytes_to_a32(key_bytes))

        if len(key_a32) == 4:
            file_key = list(_bytes_to_a32(
                _aes_cbc_decrypt(self._master_key, _a32_to_bytes(key_a32))
            ))
        else:
            file_key = [
                key_a32[0] ^ key_a32[4],
                key_a32[1] ^ key_a32[5],
                key_a32[2] ^ key_a32[6],
                key_a32[3] ^ key_a32[7],
            ]
            file_key = list(_bytes_to_a32(
                _aes_cbc_decrypt(self._master_key, _a32_to_bytes(file_key))
            ))

        # Encrypt new attributes
        new_attrs = json.dumps({"n": new_name}).encode("utf-8")
        new_attrs = b"MEGA" + new_attrs
        # Pad to 16-byte boundary
        pad = 16 - len(new_attrs) % 16
        new_attrs += b"\x00" * pad

        enc_attrs = _aes_cbc_encrypt(file_key[:4], new_attrs)
        enc_attrs_b64 = _base64_url_encode(enc_attrs)

        _api_request({
            "a": "a",
            "n": fid,
            "attr": enc_attrs_b64,
            "key": file_data.get("k", ""),
        }, sid=self._sid)
