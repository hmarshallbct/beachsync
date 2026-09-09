from fastapi.testclient import TestClient

from app import db
from app.main import app


def test_sweeps_page_lists_runs(tmp_path):
    with TestClient(app) as client:
        assert "No reports yet" in client.get("/sweeps").text
        sid = db.start_sweep("new-ids")
        db.enqueue_many("customer", "created", [31757], source="sweep")
        db.finish_sweep(sid, ok=True, summary={"customers": {"from": 31756, "queued": [31757]}, "staff": {"from": 1, "queued": []}})
        html = client.get("/sweeps").text
        assert "New-ID Sweep" in html and "31757" in html and "Completed" in html
        csv = tmp_path / "drift.csv"; csv.write_text("entity,tigerbay_id\n")
        sid = db.start_sweep("drift")
        db.finish_sweep(sid, ok=True, summary={"customer": 2, "agent": 1, "report": {"field_diff_counts": {"address": 3}}},
                        report_path=str(csv))
        html = client.get("/sweeps").text
        assert "Drift Sweep" in html and "Download the Diff CSV" in html
        assert client.get(f"/sweeps/{sid}/report").status_code == 200
        assert client.get("/sweeps/999/report").status_code == 404
