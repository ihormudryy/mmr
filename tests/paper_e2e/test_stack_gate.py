def test_autouse_stack_fixture_provides_capabilities(paper_stack):
    assert isinstance(paper_stack.capabilities, frozenset)


def test_healthz_json_ok_field(dashboard_client, paper_stack):
    response = dashboard_client.get("/healthz")
    assert response.status_code == 200, f"healthz failed: {response.status_code} {response.text}"
    assert response.json()["ok"] is True
