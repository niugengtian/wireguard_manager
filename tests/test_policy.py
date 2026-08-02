from wg_manager.policy import normalize_client_allowed_ips


def test_adjacent_half_routes_do_not_collapse_to_windows_full_tunnel():
    assert normalize_client_allowed_ips("0.0.0.0/1,128.0.0.0/1") == (
        "0.0.0.0/1, 128.0.0.0/1"
    )


def test_explicit_supernet_removes_redundant_subnet():
    assert normalize_client_allowed_ips("10.0.0.0/8,10.1.0.0/16") == "10.0.0.0/8"
