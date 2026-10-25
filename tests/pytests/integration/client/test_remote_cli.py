"""
Integration tests for remote Salt CLI client authentication.

Tests the complete client authentication flow including:
- Client key generation and management
- Authentication handshake with role="client"
- RemoteClient command execution
- Client key lifecycle (accept/reject/delete)
"""

import pytest

import salt.client
import salt.crypt
import salt.key
import salt.utils.crypt


@pytest.fixture
def client_keys(salt_master):
    """Generate client keys and clean up after test."""
    client_id = "test-remote-client"

    # Generate client keys
    client_priv, client_pub = salt.crypt.gen_keys(4096)

    # Store keys in temp location (in real usage, client keeps these)
    keys = {
        "id": client_id,
        "priv": client_priv,
        "pub": client_pub,
    }

    yield keys

    # Cleanup: delete client keys from master
    with salt.key.ClientKey(salt_master.config) as ckey:
        try:
            ckey.delete_key(client_id)
        except (KeyError, OSError):
            # Client key may not exist or be deleted already
            pass


class TestClientKeyManagement:
    """Test client key management operations."""

    def test_client_key_lifecycle(self, salt_master, client_keys):
        """Test complete client key lifecycle."""
        client_id = client_keys["id"]
        client_pub = client_keys["pub"]

        # Initialize ClientKey manager
        with salt.key.ClientKey(salt_master.config) as ckey:
            # 1. Initially, client should not exist
            all_keys = ckey.list_keys()
            assert client_id not in all_keys["clients"]
            assert client_id not in all_keys["clients_pre"]
            assert client_id not in all_keys["clients_rejected"]

            # 2. Store client public key in pending state
            ckey.cache.store(
                "client_keys", client_id, {"pub": client_pub, "state": "pending"}
            )

            # 3. Verify client appears in pending
            all_keys = ckey.list_keys()
            assert client_id in all_keys["clients_pre"]
            assert client_id not in all_keys["clients"]

            # 4. Accept the client key
            result = ckey.accept(client_id)
            assert client_id in result["clients"]

            # 5. Verify client now in accepted
            all_keys = ckey.list_keys()
            assert client_id in all_keys["clients"]
            assert client_id not in all_keys["clients_pre"]

            # 6. Reject the client key
            result = ckey.reject(client_id, include_accepted=True)
            assert client_id in result["clients_rejected"]

            # 7. Verify client now in rejected
            all_keys = ckey.list_keys()
            assert client_id in all_keys["clients_rejected"]
            assert client_id not in all_keys["clients"]

            # 8. Re-accept from rejected
            result = ckey.accept(client_id, include_rejected=True)
            assert client_id in result["clients"]

            # 9. Delete the client key
            result = ckey.delete_key(client_id)
            assert client_id in result["clients"]

            # 10. Verify client is gone
            all_keys = ckey.list_keys()
            assert client_id not in all_keys["clients"]
            assert client_id not in all_keys["clients_pre"]
            assert client_id not in all_keys["clients_rejected"]

    def test_client_key_fingerprint(self, salt_master, client_keys):
        """Test client key fingerprinting."""
        client_id = client_keys["id"]
        client_pub = client_keys["pub"]

        with salt.key.ClientKey(salt_master.config) as ckey:
            # Store and accept client key
            ckey.cache.store(
                "client_keys", client_id, {"pub": client_pub, "state": "accepted"}
            )

            # Get fingerprint
            fingerprints = ckey.finger(client_id)
            assert client_id in fingerprints["clients"]

            # Verify fingerprint format (colon-separated hex)
            fp = fingerprints["clients"][client_id]
            assert ":" in fp
            assert len(fp.split(":")) > 10  # SHA-256 has 32 bytes = 32 pairs

            # Cleanup
            ckey.delete_key(client_id)

    def test_client_key_glob_match(self, salt_master, client_keys):
        """Test glob matching for client keys."""
        client_id = client_keys["id"]
        client_pub = client_keys["pub"]

        with salt.key.ClientKey(salt_master.config) as ckey:
            # Store multiple client keys
            for i in range(3):
                ckey.cache.store(
                    "client_keys",
                    f"test-client-{i}",
                    {"pub": client_pub, "state": "accepted"},
                )

            # Test glob matching
            matches = ckey.glob_match("test-client-*")
            assert len(matches["clients"]) == 3
            assert all(f"test-client-{i}" in matches["clients"] for i in range(3))

            # Test specific match
            matches = ckey.glob_match("test-client-1")
            assert "test-client-1" in matches["clients"]
            assert len(matches["clients"]) == 1

            # Cleanup
            for i in range(3):
                ckey.delete_key(f"test-client-{i}")

    def test_client_key_print(self, salt_master, client_keys):
        """Test printing client public key."""
        client_id = client_keys["id"]
        client_pub = client_keys["pub"]

        with salt.key.ClientKey(salt_master.config) as ckey:
            # Store and accept client key
            ckey.cache.store(
                "client_keys", client_id, {"pub": client_pub, "state": "accepted"}
            )

            # Print key
            keys = ckey.key_str(client_id)
            assert client_id in keys["clients"]
            assert "-----BEGIN PUBLIC KEY-----" in keys["clients"][client_id]
            assert "-----END PUBLIC KEY-----" in keys["clients"][client_id]

            # Cleanup
            ckey.delete_key(client_id)


class TestClientAuthentication:
    """Test client authentication flow."""

    @pytest.mark.skip(reason="Requires full master/auth setup - placeholder for future")
    def test_client_auth_handshake(self, salt_master, client_keys):
        """Test client authentication handshake with role=client.

        This test would require full AsyncAuth setup.
        Placeholder for comprehensive auth testing.
        """

    @pytest.mark.skip(
        reason="Requires full master/client setup - placeholder for future"
    )
    def test_remote_client_execution(self, salt_master, client_keys):
        """Test RemoteClient command execution after authentication.

        This test would require full RemoteClient setup.
        Placeholder for comprehensive execution testing.
        """


class TestClientKeyManagementDict:
    """Test dict-based client key management."""

    def test_accept_dict(self, salt_master):
        """Test accepting client keys via dict."""
        with salt.key.ClientKey(salt_master.config) as ckey:
            # Create test keys
            clients = []
            for i in range(3):
                client_id = f"dict-client-{i}"
                _, pub = salt.crypt.gen_keys(4096)
                ckey.cache.store(
                    "client_keys", client_id, {"pub": pub, "state": "pending"}
                )
                clients.append(client_id)

            # Accept via dict
            result = ckey.accept(match_dict={"clients_pre": clients})
            assert all(c in result["clients"] for c in clients)

            # Verify all accepted
            all_keys = ckey.list_keys()
            assert all(c in all_keys["clients"] for c in clients)

            # Cleanup
            for client in clients:
                ckey.delete_key(client)

    def test_reject_dict(self, salt_master):
        """Test rejecting client keys via dict."""
        with salt.key.ClientKey(salt_master.config) as ckey:
            # Create and accept test keys
            clients = []
            for i in range(3):
                client_id = f"reject-dict-client-{i}"
                _, pub = salt.crypt.gen_keys(4096)
                ckey.cache.store(
                    "client_keys", client_id, {"pub": pub, "state": "accepted"}
                )
                clients.append(client_id)

            # Reject via dict
            result = ckey.reject(match_dict={"clients": clients}, include_accepted=True)
            assert all(c in result["clients_rejected"] for c in clients)

            # Verify all rejected
            all_keys = ckey.list_keys()
            assert all(c in all_keys["clients_rejected"] for c in clients)

            # Cleanup
            for client in clients:
                ckey.delete_key(client)

    def test_delete_dict(self, salt_master):
        """Test deleting client keys via dict."""
        with salt.key.ClientKey(salt_master.config) as ckey:
            # Create test keys
            clients = []
            for i in range(3):
                client_id = f"delete-dict-client-{i}"
                _, pub = salt.crypt.gen_keys(4096)
                ckey.cache.store(
                    "client_keys", client_id, {"pub": pub, "state": "accepted"}
                )
                clients.append(client_id)

            # Delete via dict
            result = ckey.delete_key(match_dict={"clients": clients})
            assert all(c in result["clients"] for c in clients)

            # Verify all deleted
            all_keys = ckey.list_keys()
            assert all(c not in all_keys["clients"] for c in clients)


@pytest.mark.parametrize("state", ["pending", "accepted", "rejected"])
def test_client_key_states(salt_master, state):
    """Test client keys in different states."""
    with salt.key.ClientKey(salt_master.config) as ckey:
        client_id = f"state-test-client-{state}"
        _, pub = salt.crypt.gen_keys(4096)

        # Store key in specific state
        ckey.cache.store("client_keys", client_id, {"pub": pub, "state": state})

        # Verify key in correct state
        all_keys = ckey.list_keys()
        if state == "pending":
            assert client_id in all_keys["clients_pre"]
        elif state == "accepted":
            assert client_id in all_keys["clients"]
        elif state == "rejected":
            assert client_id in all_keys["clients_rejected"]

        # Cleanup
        ckey.delete_key(client_id)
