"""Signed tokens for the claims connector (docs/claims-connector.md, "Authorising requests"): with
CLAIMS_CONNECTOR_KEY_FILE set, every request carries a fresh Ed25519-signed JWT. These tests talk to the
stand-in connector, which checks them the way a real connector must."""

import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from garagebooks import claims
from tests.fake_connector import TOKEN, Fake, load_public_keys, new_key, thumbprint


@pytest.fixture()
def keypair(tmp_path):
    private, public = new_key()
    key_file = tmp_path / "claims.key"
    key_file.write_bytes(private)
    return key_file, serialization.load_pem_public_key(public)


@pytest.fixture()
def server(keypair):
    fake = Fake(token=None, keys=[keypair[1]])
    fake.url = fake.start()
    yield fake
    fake.stop()


def unverified(fake, index=-1):
    """(header, claims) of the bearer token the connector received in a call."""
    bearer = fake.calls[index][2].removeprefix("Bearer ")
    return jwt.get_unverified_header(bearer), jwt.decode(bearer, options={"verify_signature": False})


def test_a_request_carries_a_signed_token_the_connector_accepts(server, keypair):
    reply = claims.Connector(server.url, key_file=str(keypair[0])).health()
    assert reply.ok
    header, body = unverified(server)
    assert header == {"alg": "EdDSA", "typ": "JWT", "kid": thumbprint(keypair[1])}
    assert body["iss"] == "garagebooks" and body["aud"] == "claims-connector"
    assert body["exp"] - body["iat"] == 60 and abs(body["iat"] - time.time()) < 5
    assert server.legacy_uses == 0


def test_each_request_gets_a_fresh_token(server, keypair):
    connector = claims.Connector(server.url, key_file=str(keypair[0]))
    assert connector.health().ok and connector.health().ok      # a reused jti would be a replay: the second 401s
    assert unverified(server, 0)[1]["jti"] != unverified(server, 1)[1]["jti"]


def test_the_kid_is_the_rfc_7638_thumbprint_of_the_public_key(keypair):
    # RFC 8037 appendix A.3 gives the key and its thumbprint for Ed25519.
    public = ed25519.Ed25519PublicKey.from_public_bytes(
        bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"))
    assert claims.thumbprint(public) == "kPrK_qmxVWaYVA9wwBF6Iuo3vVzz7TxHCTwXBygrS4k"
    assert claims.thumbprint(keypair[1]) == thumbprint(keypair[1])


def test_the_key_file_is_read_at_each_request_so_it_can_be_rotated(server, keypair, tmp_path):
    connector = claims.Connector(server.url, key_file=str(keypair[0]))
    assert connector.health().ok
    other, _ = new_key()
    keypair[0].write_bytes(other)                    # rotated: a key the connector doesn't know
    with pytest.raises(claims.ConnectorError, match="refused"):
        connector.health()


def test_a_refused_signed_token_says_what_to_check(server, tmp_path):
    other, _ = new_key()
    stranger = tmp_path / "other.key"
    stranger.write_bytes(other)
    with pytest.raises(claims.ConnectorError) as err:
        claims.Connector(server.url, key_file=str(stranger)).health()
    message = err.value.user_message
    assert "signed token" in message and "CLAIMS_CONNECTOR_KEY_FILE" in message
    assert "public key" in message and "clock" in message
    assert "BEGIN" not in message


def test_an_unreadable_key_file_is_explained(server, tmp_path):
    with pytest.raises(claims.ConnectorError, match="signing key file can't be read.*CLAIMS_CONNECTOR_KEY_FILE"):
        claims.Connector(server.url, key_file=str(tmp_path / "missing.key")).health()
    assert server.calls == []                         # nothing was sent without a key


@pytest.mark.parametrize("content", [
    b"not a key",
    b"",
    rsa.generate_private_key(65537, 2048).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"x")),
])
def test_a_key_file_that_is_not_an_ed25519_private_key_is_explained(server, tmp_path, content):
    bad = tmp_path / "bad.key"
    bad.write_bytes(content)
    with pytest.raises(claims.ConnectorError, match="isn't an unencrypted Ed25519 private key") as err:
        claims.Connector(server.url, key_file=str(bad)).health()
    assert "CLAIMS_CONNECTOR_KEY_FILE" in err.value.user_message
    assert server.calls == []


def test_the_key_file_wins_over_the_deprecated_token(server, keypair):
    connector = claims.Connector(server.url, token=TOKEN, key_file=str(keypair[0]))
    assert connector.health().ok
    assert unverified(server)[0]["alg"] == "EdDSA" and server.legacy_uses == 0


def test_without_a_key_file_the_deprecated_token_still_works():
    fake = Fake()
    url = fake.start()
    try:
        assert claims.Connector(url, token=TOKEN).health().ok
        assert fake.legacy_uses == 1
        with pytest.raises(claims.ConnectorError, match="refused the token"):
            claims.Connector(url, token="wrong").health()
    finally:
        fake.stop()


def test_from_env_prefers_the_key_file(keypair):
    env = {"CLAIMS_CONNECTOR_URL": "http://connector:8765", "CLAIMS_CONNECTOR_KEY_FILE": str(keypair[0]),
           "CLAIMS_CONNECTOR_TOKEN": "old"}
    connector = claims.from_env(env)
    assert connector.key_file == str(keypair[0])
    legacy = claims.from_env({"CLAIMS_CONNECTOR_URL": "http://connector:8765", "CLAIMS_CONNECTOR_TOKEN": "old"})
    assert legacy.key_file is None and legacy.token == "old"


def test_the_deprecated_token_is_logged_as_deprecated_once(caplog):
    claims.warned_legacy.clear()
    with caplog.at_level("WARNING", logger="garagebooks.claims"):
        claims.from_env({"CLAIMS_CONNECTOR_URL": "http://connector:8765", "CLAIMS_CONNECTOR_TOKEN": "old-secret"})
        claims.from_env({"CLAIMS_CONNECTOR_URL": "http://connector:8765", "CLAIMS_CONNECTOR_TOKEN": "old-secret"})
    lines = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage()]
    assert len(lines) == 1 and "CLAIMS_CONNECTOR_KEY_FILE" in lines[0] and "old-secret" not in lines[0]


# ---- the stand-in connector is a faithful verifier (third parties copy it) -----------------------------------

def sign(private_pem, kid, **override):
    now = int(time.time())
    body = {"iss": "garagebooks", "aud": "claims-connector", "iat": now, "exp": now + 60, "jti": "j" + str(now) + str(id(override))}
    body.update(override)
    body = {k: v for k, v in body.items() if v is not None}
    return jwt.encode(body, serialization.load_pem_private_key(private_pem, None), algorithm="EdDSA",
                      headers={"kid": kid})


@pytest.mark.parametrize("override", [
    {"aud": "someone-else"}, {"iss": "someone-else"}, {"exp": int(time.time()) - 120, "iat": int(time.time()) - 180},
    {"exp": int(time.time()) + 3600}, {"iat": int(time.time()) + 600, "exp": int(time.time()) + 660},
    {"jti": None},
])
def test_the_stand_in_refuses_tokens_a_real_connector_must_refuse(override):
    private, public = new_key()
    key = serialization.load_pem_public_key(public)
    fake = Fake(token=None, keys=[key])
    token = sign(private, thumbprint(key), **override)
    assert fake.handle("GET", "/v1/health", f"Bearer {token}", {})[0] == 401


def test_the_stand_in_refuses_a_replayed_token_an_unknown_kid_and_alg_none():
    private, public = new_key()
    key = serialization.load_pem_public_key(public)
    fake = Fake(token=None, keys=[key])
    token = sign(private, thumbprint(key))
    assert fake.handle("GET", "/v1/health", f"Bearer {token}", {})[0] == 200
    assert fake.handle("GET", "/v1/health", f"Bearer {token}", {})[0] == 401
    assert fake.handle("GET", "/v1/health", f"Bearer {sign(private, 'unknown')}", {})[0] == 401
    unsigned = jwt.encode({"iss": "garagebooks", "aud": "claims-connector"}, None, algorithm="none",
                          headers={"kid": thumbprint(key)})
    assert fake.handle("GET", "/v1/health", f"Bearer {unsigned}", {})[0] == 401
    assert fake.handle("GET", "/v1/health", None, {})[0] == 401


def test_public_keys_are_read_from_a_folder(tmp_path):
    _, public = new_key()
    (tmp_path / "a.pub").write_bytes(public)
    (tmp_path / "ignored.txt").write_text("x")
    assert list(load_public_keys(tmp_path)) == [thumbprint(serialization.load_pem_public_key(public))]
