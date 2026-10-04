"""One code for the settings of any package (CP-ADR-0081 §9, FR-017; SC-004, SC-006).

Two packages that are not the core's own — ``invoice-payment`` (the fixture
of ``tests/fixtures/packages``, its manifest as it is, with the settings of
the pilot of CP-ADR-0081 §1) and the test package ``sample-settings`` — are
installed, read and saved by the same routes. The answers are those the
console draws: ``GET …/settings`` of ``invoice-payment`` is the example of
§4 (``docs/settings.md`` of the console) to the letter, every answer and
error body fits the transcribed contract
(``tests/unit/test_package_settings_console_contract.py``).
"""

import copy
import uuid
from typing import Any

import httpx
import yaml

from tests.catalog_packages import PACKAGES
from tests.helpers import auth, create_role, do_bootstrap
from tests.integration.test_package_settings import (
    PACKAGE as SAMPLE,
)
from tests.integration.test_package_settings import (
    SECRET,
    declared,
    install,
    package_files,
)
from tests.unit.test_package_settings_console_contract import EXAMPLES, check

PILOT = "invoice-payment"
PILOT_SETTINGS: dict[str, Any] = {
    "schema": {
        "type": "object",
        "required": ["approverRole"],
        "properties": {
            "approvalThreshold": {"type": "number", "minimum": 0, "default": 100000},
            "reviewDueWorkdays": {"type": "integer", "minimum": 1, "maximum": 20, "default": 2},
            "approverRole": {"type": "string", "x-ref": "role"},
        },
    },
    "uischema": {
        "type": "VerticalLayout",
        "elements": [
            {
                "type": "Group",
                "label": "invoice-payment.settings.groups.approval",
                "elements": [
                    {"type": "Control", "scope": "#/properties/approvalThreshold"},
                    {"type": "Control", "scope": "#/properties/approverRole"},
                ],
            },
            {"type": "Control", "scope": "#/properties/reviewDueWorkdays"},
        ],
    },
}
PILOT_RU = {
    "invoice-payment.title": "Оплата счетов",
    "invoice-payment.settings.approvalThreshold": "Порог согласования",
    "invoice-payment.settings.approvalThreshold.help": "Сумма, выше которой нужен второй подписант",
    "invoice-payment.settings.reviewDueWorkdays": "Срок проверки, рабочих дней",
    "invoice-payment.settings.approverRole": "Роль согласующего",
    "invoice-payment.settings.groups.approval": "Согласование",
}
PILOT_EN = {
    "invoice-payment.title": "Invoice payment",
    "invoice-payment.settings.approvalThreshold": "Approval threshold",
    "invoice-payment.settings.reviewDueWorkdays": "Review due, workdays",
    "invoice-payment.settings.approverRole": "Approver role",
    "invoice-payment.settings.groups.approval": "Approval",
}


def _pilot() -> dict[str, Any]:
    """The manifest of the fixture with the settings of the pilot, and its dictionaries."""
    manifest = yaml.safe_load((PACKAGES / PILOT / "package.yaml").read_text("utf-8"))
    manifest["spec"].update(
        version="1.3.0",
        locales=["ru", "en"],
        defaultLocale="ru",
        settings=copy.deepcopy(PILOT_SETTINGS),
    )
    files = [
        ("package.yaml", yaml.safe_dump(manifest, allow_unicode=True)),
        ("i18n/ru.yaml", yaml.safe_dump(PILOT_RU, allow_unicode=True)),
        ("i18n/en.yaml", yaml.safe_dump(PILOT_EN, allow_unicode=True)),
    ]
    return {"files": [{"path": path, "content": content} for path, content in files]}


async def _put(
    client: httpx.AsyncClient, key: str, package: str, values: dict[str, Any], version: int
) -> httpx.Response:
    return await client.put(
        f"/api/v1/packages/{package}/settings",
        json={"values": values},
        headers={**auth(key), "If-Match": f'"package-settings-{version}"'},
    )


async def test_two_packages_are_served_by_one_code_in_the_form_of_the_console(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    key, admin = boot["apiKey"]["key"], boot["adminPrincipal"]["id"]
    role = (await create_role(client, key, "finance-director"))["id"]
    await install(client, key, _pilot())
    await install(client, key, package_files(settings=declared()))

    # Three savings of the pilot: the third is the example of §4.
    for version, values in enumerate(
        (
            {"approverRole": role},
            {"approverRole": role, "approvalThreshold": 120000},
            {"approvalThreshold": 150000, "approverRole": role},
        )
    ):
        saved = await _put(client, key, PILOT, values, version)
        assert saved.status_code == 200, saved.text
        check("settings", saved.json())
    assert (await _put(client, key, SAMPLE, {"owner": role, "days": 4}, 0)).status_code == 200

    read = await client.get(
        f"/api/v1/packages/{PILOT}/settings", params={"locale": "ru"}, headers=auth(key)
    )
    assert read.status_code == 200, read.text
    assert read.headers["ETag"] == '"package-settings-3"'
    body = read.json()
    check("settings", body)
    expected = copy.deepcopy(EXAMPLES["settings"])
    expected["values"]["approverRole"] = expected["effective"]["approverRole"] = role
    expected["updatedBy"] = admin
    for volatile in ("schemaHash", "updatedAt"):
        expected[volatile] = body[volatile]
    assert body == expected

    listed = (await client.get("/api/v1/package-settings", headers=auth(key))).json()
    check("list", listed)
    assert [(i["package"], i["version"]) for i in listed["items"]] == [(PILOT, 3), (SAMPLE, 1)]
    assert listed["items"][0]["title"] == "Оплата счетов"

    history = (
        await client.get(f"/api/v1/packages/{PILOT}/settings/versions", headers=auth(key))
    ).json()
    check("versions", history)
    assert history["items"][0]["changedPaths"] == ["/approvalThreshold"]
    sample = (await client.get(f"/api/v1/packages/{SAMPLE}/settings", headers=auth(key))).json()
    check("settings", sample)
    assert sample["effective"]["days"] == 4


async def test_the_refusals_of_a_saving_are_in_the_form_of_the_console(
    client: httpx.AsyncClient,
) -> None:
    boot = await do_bootstrap(client)
    key = boot["apiKey"]["key"]
    role = (await create_role(client, key, "finance-director"))["id"]
    await install(client, key, _pilot())
    cases = [
        ("secret_material_rejected", {"approverRole": role, "approvalThreshold": SECRET}),
        ("settings_invalid", {"approverRole": role, "approvalThreshold": -5}),
        ("unknown_ref", {"approverRole": str(uuid.uuid4())}),
    ]
    for code, values in cases:
        response = await _put(client, key, PILOT, values, 0)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == code
        check(code, response.json())
        assert SECRET not in response.text
    assert (await _put(client, key, PILOT, {"approverRole": role}, 0)).status_code == 200
    stale = await _put(client, key, PILOT, {"approverRole": role}, 0)
    assert stale.status_code == 409, stale.text
    check("version_conflict", stale.json())
    assert stale.json()["error"]["details"]["currentVersion"] == 1
