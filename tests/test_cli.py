from __future__ import annotations

import os
import secrets

from wg_manager import cli


def test_cli_device_lifecycle_writes_secret_file_only(monkeypatch, tmp_path, capsys):
    help_text = cli.build_parser().format_help()
    assert "本机 WireGuard" in help_text
    assert "Local WireGuard" in help_text
    data_dir = tmp_path / "cli-data"
    password = secrets.token_urlsafe(18)
    monkeypatch.setenv("WG_MANAGER_DATA_DIR", str(data_dir))
    monkeypatch.setenv("WG_COOKIE_SECURE", "0")
    monkeypatch.setattr(cli, "_new_password", lambda: password)

    cli.main(["user", "create", "cli-user", "--quota", "2"])
    output_path = tmp_path / "first.conf"
    cli.main(
        [
            "device",
            "create",
            "cli-user",
            "cli-laptop",
            "--type",
            "linux",
            "--output",
            str(output_path),
        ]
    )
    captured = capsys.readouterr()
    assert "READY:" in captured.out
    assert "PrivateKey" not in captured.out
    assert "[Interface]" not in captured.out
    assert output_path.read_text().startswith("[Interface]\nPrivateKey = ")
    assert os.stat(output_path).st_mode & 0o777 == 0o600

    cli.main(["device", "list", "--username", "cli-user"])
    listed = capsys.readouterr().out
    device_id = listed.splitlines()[1].split("\t", 1)[0]
    reset_path = tmp_path / "reset.conf"
    cli.main(["device", "reset", device_id, "--output", str(reset_path)])
    assert "PrivateKey" not in capsys.readouterr().out
    cli.main(["device", "delete", device_id])
    assert "released after peer removal" in capsys.readouterr().out
