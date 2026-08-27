from __future__ import annotations

import json
import runpy
from pathlib import Path


def test_example_resolves_then_queues_prepared_document_on_miss(
    tmp_path, monkeypatch, capsys
) -> None:
    namespace = runpy.run_path(str(Path("examples/resolve_doclang_contribute.py")))
    main = namespace["main"]
    captured = {}

    class Client:
        def resolve(self, address, **kwargs):
            captured.update(address=address, resolve=kwargs)
            return None

    class Prepared:
        def contribute(self, client, address, **kwargs):
            captured.update(client=client, contribute_address=address, contribute=kwargs)
            return {
                "status": "retained",
                "contribution_id": "sha256:" + "a" * 64,
            }

    source = tmp_path / "runbook.dclg"
    source.write_bytes(b"<doclang><text>Reusable</text></doclang>")
    monkeypatch.setitem(main.__globals__, "RDNClient", Client)
    monkeypatch.setitem(
        main.__globals__,
        "prepare_doclang_contribution",
        lambda path, validation: (
            captured.update(source=path, validation=validation) or Prepared()
        ),
    )

    assert (
        main(
            [
                str(source),
                "--uri",
                "reason://ops/deployment/rollback-plan",
                "--validation",
                "structural",
            ]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["outcome"] == "queued"
    assert output["scope"] == "local"
    assert captured["resolve"] == {"source": "chain", "scope": "local"}
    assert captured["source"] == source
    assert captured["validation"] == "structural"
    assert captured["contribute"] == {"scope": "local", "background": False}
