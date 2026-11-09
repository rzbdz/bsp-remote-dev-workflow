from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bsp.server.app import create_app
from bsp.server.config import HostConfig

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path))
    with TestClient(create_app(HostConfig.load(ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml"))) as c:
        yield c


def test_rest_routes_query_and_body(client):
    r = client.post("/api/v1/gpio/P33/set?level=low")
    assert r.status_code == 200 and r.json()["result"]["state"] == "low"
    r = client.post("/api/v1/gpio/GPIO127/set", json={"level": "high"})
    assert r.json()["result"]["state"] == "high"
    assert client.get("/api/v1/gpio/33/get").json()["result"]["state"] == "high"
    assert client.post("/api/v1/button/DUT1/reset/click", json={"ms": 10}).json()["result"]["state"] == "released"
    assert client.post("/api/v1/relay/DUT1%2Fpower/on").json()["result"]["state"] == "on"   # encoded slash works too


def test_error_envelope(client):
    r = client.post("/api/v1/gpio/DUT1/reset/set", json={"level": "high"})
    err = r.json()["error"]
    assert r.status_code == 400 and err["code"] == "invalid_request" and "button" in err["hint"]
    assert err["request"] == "POST /api/v1/gpio/DUT1/reset/set" and err["docs"] == "/llms.txt"
    r = client.post("/api/v1/gpio/DUT1/rest/set?level=low")
    assert r.status_code == 404 and "DUT1/reset" in r.json()["error"]["hint"]
    r = client.post("/api/v1/gpio/P7/sett?level=low")                    # unknown route
    assert r.status_code == 404 and r.json()["error"]["details"]["did_you_mean"]
    r = client.get("/api/v1/gpio/P7/set")                                 # wrong method
    assert r.status_code == 405 and "POST" in r.json()["error"]["hint"]
    r = client.post("/api/v1/tools/gpio_sett", json={})
    assert "gpio_set" in r.json()["error"]["hint"]
    r = client.post("/api/v1/gpio/P7/pulse", json={"ms": "abc"})          # body type error
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


def test_tools_catalogue_formats(client):
    tools = client.get("/api/v1/tools").json()["tools"]
    names = {t["name"] for t in tools}
    assert {"gpio.set", "relay.on", "button.click", "status", "events.recent"} <= names
    anth = client.get("/api/v1/tools?format=anthropic").json()
    assert all(set(t) == {"name", "description", "input_schema"} for t in anth)
    oai = client.get("/api/v1/tools?format=openai").json()
    assert oai[0]["type"] == "function"
    r = client.post("/api/v1/tools/gpio_set", json={"target": "P7", "level": "low"}).json()
    assert r["ok"] and r["command"] == "gpio.set" and "P7" in r["text"]


def test_cli_endpoint(client):
    r = client.post("/api/v1/cli", json={"line": "gpio list"}).json()
    assert r["exit_code"] == 0 and "DUT1/reset" in r["output"]
    r = client.post("/api/v1/cli", json={"line": "gpio alias add DUT9/reset P7 --owner test"}).json()
    assert r["exit_code"] == 0 and "P7" in r["output"]
    assert "DUT9/reset" in client.post("/api/v1/cli", json={"line": "gpio alias list DUT9"}).json()["output"]
    r = client.post("/api/v1/cli", json={"line": "gpio alias"}).json()
    assert r["exit_code"] == 0 and "add" in r["output"]                   # group help
    r = client.post("/api/v1/cli", json={"line": "gpio set P3"}).json()
    assert r["exit_code"] == 2 and "required" in r["output"]


def test_misc_pages(client):
    assert "gpio_set" in client.get("/llms.txt").text
    html = client.get("/").text
    assert "__V__" not in html and "app.js?v=" in html
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/openapi.json").json()["paths"]["/api/v1/gpio/{target}/set"]["post"]
    assert client.get("/api/v1/events/recent").json()["result"][0]["topic"]


def test_auth_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BSP_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BSP_TOKEN", "s3cret")
    with TestClient(create_app(HostConfig.load(ROOT / "examples" / "site" / "hosts" / "dev-mock.yaml"))) as c:
        assert c.get("/api/v1/gpio/list").status_code == 200
        assert c.post("/api/v1/gpio/P7/set?level=low").status_code == 403
        assert c.post("/api/v1/gpio/P7/set?level=low", headers={"Authorization": "Bearer s3cret"}).status_code == 200
