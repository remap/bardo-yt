import ssl

import pytest

from ytmatrix import certs


def test_falls_back_to_self_signed_when_mkcert_is_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(certs.shutil, "which", lambda name: None)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"

    assert certs.ensure_cert(cert, key) == "self-signed"
    assert cert.exists() and key.exists()


def test_falls_back_when_mkcert_exists_but_its_ca_is_not_installed(tmp_path, monkeypatch):
    # mkcert on PATH proves nothing: without `mkcert -install` its certificates
    # are exactly as untrusted as a self-signed one.
    monkeypatch.setattr(certs.shutil, "which", lambda name: "/usr/local/bin/mkcert")
    monkeypatch.setattr(certs, "mkcert_ca_installed", lambda: False)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"

    assert certs.ensure_cert(cert, key) == "self-signed"


def test_existing_certificates_are_left_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(certs.shutil, "which", lambda name: None)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    certs.ensure_cert(cert, key)
    original = cert.read_bytes()

    assert certs.ensure_cert(cert, key) == "existing"
    assert cert.read_bytes() == original


def test_a_failing_mkcert_still_yields_a_usable_certificate(tmp_path, monkeypatch):
    monkeypatch.setattr(certs, "mkcert_ca_installed", lambda: True)

    def boom(*args, **kwargs):
        raise OSError("mkcert exploded")

    monkeypatch.setattr(certs.subprocess, "run", boom)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"

    # A broken mkcert must not take the server down with it.
    assert certs.ensure_cert(cert, key) == "self-signed"
    assert cert.exists()


def test_the_certificate_covers_localhost(tmp_path, monkeypatch):
    # localhost specifically: YouTube rejects 127.0.0.1 as an embed origin.
    monkeypatch.setattr(certs.shutil, "which", lambda name: None)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    certs.ensure_cert(cert, key)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)  # raises if the pair does not match

    names = [
        value
        for entry in ssl._ssl._test_decode_cert(str(cert))["subjectAltName"]
        for value in [entry[1]]
    ]
    assert "localhost" in names


def test_mkcert_ca_installed_is_false_without_mkcert(monkeypatch):
    monkeypatch.setattr(certs.shutil, "which", lambda name: None)
    assert certs.mkcert_ca_installed() is False


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1"])
def test_cert_hosts_includes_every_address_the_server_binds(host):
    assert host in certs.CERT_HOSTS
