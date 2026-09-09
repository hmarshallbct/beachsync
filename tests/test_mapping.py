from app import mapping


def test_map_customer_uses_primary_contact_and_country_name(tb):
    tb.add_customer(1000)
    props = mapping.map_customer(tb.customer_profile(1000))
    assert props["email"] == "jane1000@example.com"
    assert props["firstname"] == "Jane" and props["lastname"] == "Doe"
    assert "date_of_birth" not in props and "country" not in props
    assert props["mobilephone"] == "07700900001"
    assert props["city"] == "Bath" and props["zip"] == "BA1 1AA"
    assert props["tigerbay_customer_id"] == "1000" and props["tigerbay_id"] == "1000"
    assert props["title"] == "Mrs" and props["county"] == "Somerset"
    assert props["cancel_from_email"] == "FALSE" and props["is_archived"] == "FALSE"


def test_map_customer_ignores_preprod_placeholders(tb):
    tb.add_customer(2, contacts=[{"Type": "Primary", "Address0": "housename", "TownCity": "townCity",
                                  "County": "county", "PostCode": "postcode", "Country": "GBR",
                                  "PersonalMobile": "personalMobile", "PersonalLandline": "personalLandline"}])
    props = mapping.map_customer(tb.customer_profile(2))
    assert props["address"] == "" and props["city"] == "" and props["mobilephone"] == ""


def test_map_staff_splits_name_and_links_agency(tb):
    tb.add_agency(500, name="Best Travel")
    tb.add_staff(501, 500, name="Mike de Brown", email="Mike@Best.example")
    props = mapping.map_staff(tb.agent_profile(501))
    assert props["firstname"] == "Mike de" and props["lastname"] == "Brown"
    assert props["email"] == "mike@best.example"
    assert props["agency_name"] == "Best Travel" and props["abta_reference"] == "P500"
    assert props["tigerbay_id"] == "500" and props["tigerbay_agent_id"] == "501"
    assert props["is_archived"] == "FALSE"


def test_map_agency(tb):
    tb.add_agency(500)
    props = mapping.map_agency(tb.agents[500], tb.agent_contacts[500])
    assert props["name"] == "Best Travel" and props["tigerbay_id"] == "500" and props["abta_reference"] == "P500"
    assert props["phone"] == "01130000000" and props["address"] == "Unit 2, Park Rd"
    assert "mobilephone" not in props


def test_diff_only_changed_and_never_clear():
    desired = {"firstname": "Jane", "lastname": "Doe", "phone": "", "cancel_from_email": "FALSE", "is_archived": "FALSE"}
    current = {"firstname": "jane", "lastname": "Doe", "phone": "0123", "cancel_from_email": "", "is_archived": "false"}
    d = mapping.diff(desired, current)
    # phone kept (NEVER_CLEAR); is_archived equal case-insensitively; cancel_from_email filled in
    assert d == {"firstname": "Jane", "cancel_from_email": "FALSE"}


def test_diff_new_record_drops_blanks():
    assert mapping.diff({"a": "1", "b": "", "c": None}, None) == {"a": "1"}


def test_diff_email_case_insensitive():
    assert mapping.diff({"email": "A@B.com"}, {"email": "a@b.com"}) == {}


def test_split_name_strips_suffixes():
    assert mapping.split_name("Melanie Harper - Travel Consultant") == ("Melanie", "Harper")
    assert mapping.split_name("Mark Ferrier (Clydebank)") == ("Mark", "Ferrier")
    assert mapping.split_name("Ruth") == ("Ruth", "")
    assert mapping.split_name("Anne-Marie Smith") == ("Anne-Marie", "Smith")


def test_staff_falls_back_to_agency_address(tb):
    tb.add_agency(500)
    tb.add_staff(501, 500)
    props = mapping.map_staff(tb.agent_profile(501))
    assert props["address"] == "Unit 2, Park Rd" and props["city"] == "Leeds" and props["phone"] == "01130000000"


def test_phone_digits_compare_and_names_never_cleared():
    d = mapping.diff({"phone": "07796050100", "lastname": ""}, {"phone": "07796 050100.", "lastname": "Smith"})
    assert d == {}


def test_names_compatible():
    nc = mapping.names_compatible
    assert nc({"firstname": "Pat", "lastname": "Wright"}, {"firstname": "", "lastname": ""})
    assert nc({"firstname": "Pat", "lastname": "Wright"}, {"firstname": "Patricia", "lastname": "Wright"})
    assert nc({"firstname": "Marie Danielle", "lastname": "Jameson"}, {"firstname": "Danielle", "lastname": "Jameson"})
    assert not nc({"firstname": "Leslie", "lastname": "Barker"}, {"firstname": "Sarah", "lastname": "Archer"})
    assert not nc({"firstname": "Reece", "lastname": "Milton"}, {"firstname": "", "lastname": "Cannon"})


def test_opt_out_never_cleared_but_can_be_set():
    d = mapping.diff({"cancel_from_email": "FALSE", "cancel_from_mailing": "TRUE"},
                     {"cancel_from_email": "TRUE", "cancel_from_mailing": "FALSE"})
    assert d == {"cancel_from_mailing": "TRUE"}
