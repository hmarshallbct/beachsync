from app import webhook


def test_extract_from_path():
    assert webhook.extract("customer", "modified", "123", None, {}) == ("customer", "modified", 123)


def test_extract_from_body_keys():
    body = {"EventType": "Created", "EntityType": "Customer", "EntityId": "456"}
    assert webhook.extract(None, None, None, body, {}) == ("customer", "created", 456)


def test_extract_full_record_body_archived_flag():
    body = {"Id": 22926, "BundleReference": "22926-1110", "Archived": True}
    assert webhook.extract("clients", None, None, body, {}) == ("customer", "archived", 22926)


def test_extract_nested_and_links():
    body = {"Data": {"Customer": {"Links": [{"Href": "https://x/nimble/sales/customers/77"}]}}}
    ent, ev, eid = webhook.extract("customer", "modified", None, body, {})
    assert eid == 77


def test_extract_query_id_and_agent_alias():
    assert webhook.extract("agentstaff", "archive", None, {}, {"id": "9"}) == ("agent", "archived", 9)


def test_parse_body_forms_and_bare():
    assert webhook.parse_body(b"123") == {"Id": 123}
    assert webhook.parse_body(b"Id=5&EventType=Modified") == {"Id": "5", "EventType": "Modified"}
    assert webhook.parse_body(b"") is None


def test_extract_real_tigerbay_shape():
    body = {"subject": "Customers_CustomerModified",
            "data": [{"key": "CustomerId", "value": 2732},
                     {"key": "Diagnostic-Id", "value": "00-bbdbd0f4306f826b876675ed5bec1110-10b2895d1af19776-00"}]}
    assert webhook.extract("customer", "modified", None, body, {}) == ("customer", "modified", 2732)
    # entity/event derivable from the subject alone
    assert webhook.extract(None, None, None, body, {}) == ("customer", "modified", 2732)
    body = {"subject": "Agents_AgentStaffArchived", "data": [{"key": "AgentStaffId", "value": "744"}]}
    assert webhook.extract(None, None, None, body, {}) == ("agent", "archived", 744)
    body = {"subject": "Agents_AgentCreated", "data": [{"key": "AgentId", "value": 669}]}
    assert webhook.extract("agent", "created", None, body, {}) == ("agent", "created", 669)
