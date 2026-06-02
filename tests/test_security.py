from datetime import timedelta

import pytest
from cryptography.fernet import Fernet
from jose import JWTError

from app.core.security import (
    DecryptionError,
    create_access_token,
    decode_access_token,
    decrypt_token,
    encrypt_token,
)


@pytest.fixture(autouse=True)
def patch_fernet(monkeypatch, fernet_key):
    monkeypatch.setattr("app.core.config.settings.FERNET_KEY", fernet_key)
    monkeypatch.setattr("app.core.security._fernet", None)


@pytest.fixture(autouse=True)
def patch_secret(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.SECRET_KEY", "test-secret-key")


def test_encrypt_decrypt_roundtrip():
    plaintext = "my_super_secret_token_xyz"
    ciphertext = encrypt_token(plaintext)
    assert ciphertext != plaintext
    assert decrypt_token(ciphertext) == plaintext


def test_encrypt_produces_different_output_each_time():
    plaintext = "same-input"
    assert encrypt_token(plaintext) != encrypt_token(plaintext)


def test_decrypt_invalid_token_raises():
    with pytest.raises(DecryptionError):
        decrypt_token("not-a-valid-fernet-token")


def test_decrypt_tampered_ciphertext_raises(fernet_key):
    ciphertext = encrypt_token("original")
    tampered = ciphertext[:-4] + "XXXX"
    with pytest.raises(DecryptionError):
        decrypt_token(tampered)


def test_create_access_token_includes_exp():
    token = create_access_token({"sub": "user-1"})
    payload = decode_access_token(token)
    assert "exp" in payload
    assert payload["sub"] == "user-1"


def test_decode_valid_token():
    token = create_access_token({"sub": "user-42", "email": "u@example.com"})
    payload = decode_access_token(token)
    assert payload["sub"] == "user-42"
    assert payload["email"] == "u@example.com"


def test_decode_expired_token_raises():
    token = create_access_token({"sub": "user-1"}, expires_delta=timedelta(seconds=-1))
    with pytest.raises(JWTError):
        decode_access_token(token)


def test_decode_tampered_token_raises():
    token = create_access_token({"sub": "user-1"})
    tampered = token[:-5] + "XXXXX"
    with pytest.raises(JWTError):
        decode_access_token(tampered)


def test_fernet_key_validation_rejects_bad_key():
    from pydantic import ValidationError
    from app.core.config import Settings

    with pytest.raises((ValidationError, ValueError)):
        Settings(FERNET_KEY="not-a-valid-fernet-key")
