def test_landing_page_renders(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Slack Channel Export" in resp.data
