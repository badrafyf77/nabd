"""Authentication, REST error handling and the customer-scoped read adapter.

Uses httpx.MockTransport, so nothing leaves the machine.
"""
import asyncio
from datetime import date
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
from pydantic import SecretStr, ValidationError
import pytest

from nabd.salesforce import soql
from nabd.salesforce.reads import SalesforceReads
from nabd.salesforce.rest import (
    AccessToken, ClientCredentialsAuth, SalesforceAuthError, SalesforceClient, SalesforceRequestError,
    SalesforceUnavailable,
)
from nabd.settings import SalesforceSettings

DOMAIN = "https://nabd-test.my.salesforce.com"
CLIENT_ID = "test-client-id-value"
SECRET = "test-client-secret-value"
STAMP = "2026-11-18T09:00:00.000+0000"


class FakeOrg:
    """Token endpoint plus a scripted REST API that records every request."""

    def __init__(self, *, token_status: int = 200, expire_first: int = 0):
        self.token_status = token_status
        self.expire_first = expire_first
        self.tokens = 0
        self.queries: list[str] = []
        self.responses: list[httpx.Response | Exception] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/services/oauth2/token":
            self.tokens += 1
            form = parse_qs(request.content.decode())
            if self.token_status != 200 or form["client_secret"] != [SECRET]:
                return httpx.Response(400, json={"error": "invalid_client",
                                                 "error_description": "invalid client credentials"})
            return httpx.Response(200, json={"access_token": f"token-{self.tokens}", "instance_url": DOMAIN,
                                             "token_type": "Bearer"})
        if self.expire_first:
            self.expire_first -= 1
            return httpx.Response(401, json=[{"errorCode": "INVALID_SESSION_ID", "message": "expired"}])
        if "q" in request.url.params:
            self.queries.append(request.url.params["q"])
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _client(org: FakeOrg, secret: str = SECRET):
    http = httpx.AsyncClient(transport=httpx.MockTransport(org.handler))
    auth = ClientCredentialsAuth(DOMAIN, SecretStr(CLIENT_ID), SecretStr(secret), http)
    return http, auth, SalesforceClient(auth, http, "68.0")


def _records(*records, done=True, next_url=None):
    body = {"totalSize": len(records), "done": done, "records": [{"attributes": {"type": "X"}, **r} for r in records]}
    if next_url:
        body["nextRecordsUrl"] = next_url
    return httpx.Response(200, json=body)


def test_tokens_are_cached_and_never_printed():
    org = FakeOrg()

    async def main():
        http, auth, _ = _client(org)
        async with http:
            first, second = await auth.token(), await auth.token()
            return first, second, repr(auth), repr(first)

    first, second, auth_repr, token_repr = asyncio.run(main())
    assert first is second and org.tokens == 1
    assert "token-1" not in token_repr and SECRET not in auth_repr and "token-1" not in auth_repr


def test_rejected_credentials_raise_without_leaking_secrets():
    org = FakeOrg()

    async def main():
        http, auth, _ = _client(org, secret="a-wrong-secret")
        async with http:
            await auth.token()

    with pytest.raises(SalesforceAuthError) as error:
        asyncio.run(main())
    text = f"{error.value} {error.value!r}"
    assert "invalid_client" in text and "a-wrong-secret" not in text and CLIENT_ID not in text


def test_expired_session_is_refreshed_once_then_reported():
    org = FakeOrg(expire_first=1)
    org.responses = [_records({"Id": "1"})]

    async def main():
        http, _, client = _client(org)
        async with http:
            return await client.query("SELECT Id FROM Account")

    assert asyncio.run(main()) == [{"Id": "1"}] and org.tokens == 2

    org = FakeOrg(expire_first=2)
    with pytest.raises(SalesforceAuthError):
        asyncio.run(main())


def test_outages_and_refusals_are_typed_errors_not_fallback_data():
    async def query(responses):
        org = FakeOrg()
        org.responses = responses
        http, _, client = _client(org)
        async with http:
            return await client.query("SELECT Id FROM Account")

    with pytest.raises(SalesforceUnavailable):
        asyncio.run(query([httpx.ConnectTimeout("slow")]))
    with pytest.raises(SalesforceUnavailable):
        asyncio.run(query([httpx.Response(503, text="maintenance")]))
    with pytest.raises(SalesforceRequestError) as error:
        asyncio.run(query([httpx.Response(400, json=[{"errorCode": "INVALID_FIELD", "message": "No such column"}])]))
    assert error.value.codes == ("INVALID_FIELD",)


def test_query_follows_result_pages_and_flattens_subqueries():
    org = FakeOrg()
    org.responses = [
        _records({"Id": "1", "Items__r": {"totalSize": 1, "done": True,
                                           "records": [{"attributes": {}, "Id": "i"}]}},
                 done=False, next_url="/services/data/v68.0/query/01g-2000"),
        _records({"Id": "2", "Items__r": None}),
    ]

    async def main():
        http, _, client = _client(org)
        async with http:
            return await client.query("SELECT Id FROM ShopEasy_Order__c")

    assert asyncio.run(main()) == [{"Id": "1", "Items__r": [{"Id": "i"}]}, {"Id": "2", "Items__r": None}]


def test_collection_failures_are_reported():
    org = FakeOrg()
    org.responses = [httpx.Response(200, json=[{"success": False, "errors": [
        {"statusCode": "DUPLICATE_VALUE", "message": "duplicate value found"}]}])]

    async def main():
        http, _, client = _client(org)
        async with http:
            await client.upsert("Account", "ShopEasy_Customer_Id__c", [{"ShopEasy_Customer_Id__c": "C001"}])

    with pytest.raises(SalesforceRequestError, match="DUPLICATE_VALUE"):
        asyncio.run(main())


def _order_row(number="SE-1081"):
    return {
        "Id": "a01", "SystemModstamp": STAMP, "Order_Number__c": number, "Order_Date__c": "2026-11-11",
        "Status__c": "delivered", "Total_GBP__c": 54.98, "Payment_Method__c": "Visa ending 5560",
        "Deliveries__r": {"totalSize": 1, "done": True, "records": [{
            "Id": "a02", "SystemModstamp": STAMP, "Status__c": "delivered", "Carrier__c": "SwiftShip",
            "Tracking_Number__c": "RP1081005GB", "Shipped_Date__c": "2026-11-12", "Expected_Date__c": "2026-11-13",
            "Original_Expected_Date__c": "2026-11-13", "Delivered_Date__c": "2026-11-13",
            "Last_Location__c": "Delivered to front door", "Last_Update_Date__c": "2026-11-13"}]},
        "Items__r": {"totalSize": 2, "done": True, "records": [
            {"Id": "a04", "SystemModstamp": STAMP, "Name": "Linen shirt", "Line_Number__c": 2.0,
             "Category__c": "clothing", "Quantity__c": 1.0, "Unit_Price_GBP__c": 34.99, "Returnable__c": True},
            {"Id": "a03", "SystemModstamp": STAMP, "Name": "Cotton boxer briefs, 3-pack", "Line_Number__c": 1.0,
             "Category__c": "underwear", "Quantity__c": 1.0, "Unit_Price_GBP__c": 19.99, "Returnable__c": False},
        ]},
    }


def test_order_reads_are_scoped_to_the_trusted_customer():
    org = FakeOrg()
    org.responses = [_records(_order_row()), _records()]

    async def main():
        http, _, client = _client(org)
        async with http:
            reads = SalesforceReads(client)
            return await reads.get_order("C005", "SE-1081"), await reads.get_order("C001", "SE-1081")

    mine, theirs = asyncio.run(main())
    assert theirs is None
    assert "Account__r.ShopEasy_Customer_Id__c = 'C005'" in org.queries[0]
    assert "Account__r.ShopEasy_Customer_Id__c = 'C001'" in org.queries[1]
    assert mine.total_gbp == Decimal("54.98") and mine.order_date == date(2026, 11, 11)
    assert [(item.line_number, item.returnable) for item in mine.items] == [(1, False), (2, True)]
    assert mine.delivery.status == "delivered" and mine.record.id == "a01"


@pytest.mark.parametrize(("customer", "order"), [
    ("C001' OR Name != '", "SE-1042"), ("C001", "SE-1042' OR Id != '"), ("", "SE-1042"), ("c001", "SE-1042"),
])
def test_malformed_identifiers_are_rejected_before_any_request(customer, order):
    org = FakeOrg()

    async def main():
        http, _, client = _client(org)
        async with http:
            await SalesforceReads(client).get_order(customer, order)

    with pytest.raises(soql.InvalidReference):
        asyncio.run(main())
    assert org.tokens == 0 and org.queries == []


def test_literals_are_escaped_even_after_validation():
    assert soql.literal("it's \\ fine\n") == "'it\\'s \\\\ fine\\n'"


def test_adapter_cannot_select_fields_the_integration_user_lacks():
    with pytest.raises(ValueError, match="not readable"):
        soql.select("Case", "Sentiment__c")
    with pytest.raises(KeyError):
        soql.select("Case", "Made_Up__c")
    assert soql.select("Case", "ShopEasy_Order__r.Order_Number__c") == "ShopEasy_Order__r.Order_Number__c"


def _knowledge_row(key, revision, body):
    return {"Id": f"k{revision}", "SystemModstamp": STAMP, "Name": key, "Entry_Key__c": key,
            "Revision__c": float(revision), "Revision_Key__c": f"{key}@{revision}", "Entry_Type__c": "faq",
            "Question__c": "Q?", "Body__c": body, "Topic__c": "t", "Language__c": "en",
            "Published_At__c": STAMP}


def test_knowledge_reads_request_published_revisions_and_keep_the_newest():
    org = FakeOrg()
    org.responses = [_records(_knowledge_row("faq.a", 3, "new"), _knowledge_row("faq.a", 2, "old"),
                              _knowledge_row("faq.b", 1, "b"))]

    async def main():
        http, _, client = _client(org)
        async with http:
            return await SalesforceReads(client).list_published_knowledge("faq")

    entries = asyncio.run(main())
    assert "Status__c = 'Published'" in org.queries[0] and "Entry_Type__c = 'faq'" in org.queries[0]
    assert [(entry.entry_key, entry.revision, entry.body) for entry in entries] == [
        ("faq.a", 3, "new"), ("faq.b", 1, "b")]


def test_salesforce_settings_validate_the_domain_and_redact_credentials():
    settings = SalesforceSettings(_env_file=None, domain_url=f"{DOMAIN}/", client_id=CLIENT_ID,
                                  client_secret=SECRET)
    assert settings.domain_url == DOMAIN
    assert SECRET not in repr(settings) and CLIENT_ID not in settings.model_dump_json()
    for bad in ("http://nabd.my.salesforce.com", "https://login.salesforce.com", f"{DOMAIN}/path"):
        with pytest.raises(ValidationError):
            SalesforceSettings(_env_file=None, domain_url=bad, client_id=CLIENT_ID, client_secret=SECRET)
    with pytest.raises(ValidationError) as error:
        SalesforceSettings(_env_file=None, domain_url=DOMAIN, client_id="", client_secret=SECRET)
    assert SECRET not in str(error.value)


def test_access_token_repr_hides_the_value():
    token = AccessToken(SecretStr("00Dabc!secret"), DOMAIN)
    assert "secret" not in repr(token) and "secret" not in str(token)


def test_staff_reads_query_only_public_comments_and_current_owner_without_a_history_limit():
    org = FakeOrg()
    case_id = '500000000000001AAA'
    org.responses = [_records({
        'Id':case_id, 'SystemModstamp':STAMP, 'CaseNumber':'00002001', 'Status':'Working', 'IsClosed':False,
        'Account':{'ShopEasy_Customer_Id__c':'C003'}, 'Resolution_Outcome__c':None,
        'Staff_Action_Required__c':'refund', 'Staff_Action_Completed__c':True}),
        _records({'Id':'00a000000000001AAA','ParentId':case_id,'CommentBody':'Public reply',
                  'CreatedDate':STAMP,'LastModifiedDate':STAMP})]
    async def run():
        http, _, client = _client(org)
        async with http:
            return await SalesforceReads(client).case_states([case_id])
    states = asyncio.run(run())
    assert states[case_id].customer_id == 'C003'
    assert states[case_id].replies[0].body == 'Public reply'
    assert 'FROM CaseComment' in org.queries[1] and 'IsPublished = true' in org.queries[1]
    assert 'LIMIT' not in org.queries[1]
    assert not any(word in ' '.join(org.queries) for word in ('Sentiment', 'Risk', 'Negative_Streak', 'CreatedBy'))
