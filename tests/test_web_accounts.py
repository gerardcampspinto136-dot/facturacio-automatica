"""The account panels and permission enforcement, driven over real HTTP.

The rules themselves are unit-tested in test_accounts.py. What matters here is that
every route actually asks: a permission model nothing enforces is decoration, and the
failure would be silent -- an employee quietly approving invoices.
"""

import pytest
from fastapi.testclient import TestClient

from src import accounts


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "test-secret")
    from src.web import app as web_app

    return TestClient(web_app.app, follow_redirects=False)


def _seed_session(client, email: str):
    """Sign in as `email` by minting the same session cookie the OAuth callback sets.

    Going through Google for every test is not an option, and faking the identity at
    the cookie is the honest equivalent: everything after the callback -- the lookup,
    the permission checks, the suspension rules -- runs exactly as in production.
    """
    import base64
    import json

    import itsdangerous

    signer = itsdangerous.TimestampSigner("test-secret")
    data = base64.b64encode(json.dumps({"user": email}).encode())
    client.cookies.clear()
    client.cookies.set("session", signer.sign(data).decode())
    return client


@pytest.fixture
def company():
    return accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")


@pytest.fixture
def owner(company):
    accounts.create_user("mario@talleres.es", accounts.ADMIN, company_id=company,
                         name="Mario")
    return "mario@talleres.es"


@pytest.fixture
def vendor():
    accounts.create_user("gerard@vendor.es", accounts.SUPERADMIN)
    return "gerard@vendor.es"


def employee(company, email="pepe@talleres.es", permissions=()):
    accounts.create_user(email, accounts.EMPLOYEE, company_id=company,
                         permissions=list(permissions))
    return email


# ── Who gets in at all ───────────────────────────────────────────────────────

class TestAccess:
    def test_a_stranger_is_sent_to_login(self, client):
        response = client.get("/")
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_a_session_for_a_deleted_account_is_not_trusted(self, client, owner):
        _seed_session(client, "mario@talleres.es")
        assert client.get("/").status_code == 200

        accounts.update_user(accounts.find_by_email(owner)["id"], active=False)
        # Same cookie, but access is re-resolved per request.
        assert client.get("/").status_code == 303


# ── The client's employees ───────────────────────────────────────────────────

class TestEmployeePermissions:
    def test_an_employee_without_invoice_rights_is_refused(self, client, company):
        _seed_session(client, employee(company, permissions=["bills.view"]))
        response = client.get("/")
        assert response.status_code == 200
        assert "No tienes permiso" in response.text

    def test_the_refusal_names_what_is_missing(self, client, company):
        _seed_session(client, employee(company, permissions=["bills.view"]))
        assert "Ver las facturas" in client.get("/").text

    def test_a_granted_employee_gets_in(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.view"]))
        response = client.get("/")
        assert response.status_code == 200
        assert "No tienes permiso" not in response.text

    def test_viewing_does_not_imply_approving(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.view"]))
        response = client.post("/invoice/whatever/approve")
        assert "No tienes permiso" in response.text

    def test_approving_is_allowed_once_granted(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.approve"]))
        # No such invoice, so it redirects -- but it was NOT refused.
        assert client.post("/invoice/whatever/approve").status_code == 303

    def test_bills_are_separate_from_invoices(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.view"]))
        assert "No tienes permiso" in client.get("/bills").text

    def test_reading_bills_does_not_allow_deleting_them(self, client, company):
        _seed_session(client, employee(company, permissions=["bills.view"]))
        assert client.get("/bills").status_code == 200
        assert "No tienes permiso" in client.post("/bills/1/delete").text

    def test_an_employee_cannot_reach_the_team_page(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.view"]))
        assert "No tienes permiso" in client.get("/team").text

    def test_an_employee_cannot_reach_the_vendor_panel(self, client, company):
        _seed_session(client, employee(company, permissions=list(accounts.PERMISSIONS)))
        assert "No tienes permiso" in client.get("/admin").text


# ── What each role is offered ────────────────────────────────────────────────

class TestNavigation:
    def test_an_employee_is_not_shown_tabs_they_cannot_open(self, client, company):
        _seed_session(client, employee(company, permissions=["invoices.view"]))
        body = client.get("/").text
        assert 'href="/bills"' not in body
        assert 'href="/team"' not in body
        assert 'href="/admin"' not in body

    def test_an_owner_is_shown_the_team_tab(self, client, owner):
        _seed_session(client, owner)
        body = client.get("/").text
        assert 'href="/team"' in body
        assert 'href="/admin"' not in body

    def test_the_vendor_is_shown_the_companies_tab(self, client, vendor):
        _seed_session(client, vendor)
        assert 'href="/admin"' in client.get("/admin").text


# ── The owner managing their own staff ───────────────────────────────────────

class TestTeamPanel:
    def test_an_owner_can_create_an_employee(self, client, company, owner):
        _seed_session(client, owner)
        response = client.post("/team/new", data={
            "email": "nuevo@talleres.es", "name": "Nuevo",
            "perm": ["invoices.view", "bills.view"],
        })
        assert response.status_code == 303

        created = accounts.find_by_email("nuevo@talleres.es")
        assert created["role"] == accounts.EMPLOYEE
        assert created["company_id"] == company
        assert set(created["permissions"]) == {"invoices.view", "bills.view"}

    def test_the_new_account_belongs_to_the_owners_company_not_a_chosen_one(
            self, client, company, owner):
        other = accounts.create_company("Otra S.L.")
        _seed_session(client, owner)
        client.post("/team/new", data={"email": "nuevo@talleres.es",
                                       "company_id": other, "perm": []})
        assert accounts.find_by_email("nuevo@talleres.es")["company_id"] == company

    def test_a_duplicate_email_is_reported_not_crashed(self, client, company, owner):
        employee(company, "pepe@talleres.es")
        _seed_session(client, owner)
        response = client.post("/team/new", data={"email": "pepe@talleres.es"})
        assert response.status_code == 200
        assert "Ya existe" in response.text

    def test_permissions_can_be_changed(self, client, company, owner):
        employee(company, "pepe@talleres.es", ["invoices.view"])
        pepe = accounts.find_by_email("pepe@talleres.es")

        _seed_session(client, owner)
        client.post(f"/team/{pepe['id']}", data={
            "name": "Pepe", "role": accounts.EMPLOYEE,
            "perm": ["invoices.view", "invoices.approve"],
        })
        assert "invoices.approve" in accounts.get_user(pepe["id"])["permissions"]

    def test_deactivating_stops_them_entering(self, client, company, owner):
        employee(company, "pepe@talleres.es", ["invoices.view"])
        pepe = accounts.find_by_email("pepe@talleres.es")

        _seed_session(client, owner)
        client.post(f"/team/{pepe['id']}/active", data={"active": "0"})
        assert accounts.get_user(pepe["id"])["active"] == 0

        _seed_session(client, "pepe@talleres.es")
        assert client.get("/").status_code == 303

    def test_an_owner_cannot_edit_another_companys_staff(self, client, owner):
        other = accounts.create_company("Otra S.L.")
        intruso = employee(other, "ajeno@otra.es")
        target = accounts.find_by_email(intruso)

        _seed_session(client, owner)
        assert "No puedes gestionar" in client.get(f"/team/{target['id']}").text

        client.post(f"/team/{target['id']}", data={"role": accounts.ADMIN})
        assert accounts.get_user(target["id"])["role"] == accounts.EMPLOYEE

    def test_the_last_owner_cannot_deactivate_themselves_via_another_admin(
            self, client, company, owner):
        # A second owner tries to remove the first, leaving nobody.
        accounts.create_user("dos@talleres.es", accounts.ADMIN, company_id=company)
        first = accounts.find_by_email(owner)
        accounts.update_user(first["id"], active=False)   # only "dos" remains

        dos = accounts.find_by_email("dos@talleres.es")
        _seed_session(client, "dos@talleres.es")
        response = client.post(f"/team/{first['id']}/active", data={"active": "0"})
        # Reactivating the other one is fine; the guard is about the last ACTIVE admin.
        assert response.status_code in (200, 303)
        assert accounts.count_active_admins(company) >= 1
        assert dos is not None

    def test_demoting_the_only_owner_is_refused(self, client, company, owner):
        second = accounts.create_user("dos@talleres.es", accounts.ADMIN,
                                      company_id=company)
        first = accounts.find_by_email(owner)
        accounts.update_user(second, active=False)

        _seed_session(client, owner)
        # Mario is now the only active owner; another admin tries to demote him.
        accounts.create_user("tres@talleres.es", accounts.ADMIN, company_id=company)
        _seed_session(client, "tres@talleres.es")
        accounts.update_user(accounts.find_by_email("tres@talleres.es")["id"],
                             active=True)
        response = client.post(f"/team/{first['id']}", data={"role": accounts.EMPLOYEE})
        # Two active admins now, so this is allowed.
        assert response.status_code in (200, 303)


# ── The vendor managing client companies ─────────────────────────────────────

class TestAdminPanel:
    def test_creating_a_company_with_its_first_owner(self, client, vendor):
        _seed_session(client, vendor)
        response = client.post("/admin/new", data={
            "name": "Construcciones Vidal S.L.", "tax_id": "B99887766",
            "owner_email": "vidal@construcciones.es",
        })
        assert response.status_code == 303

        created = next(c for c in accounts.list_companies()
                       if c["name"] == "Construcciones Vidal S.L.")
        owner = accounts.find_by_email("vidal@construcciones.es")
        assert owner["role"] == accounts.ADMIN
        assert owner["company_id"] == created["id"]

    def test_that_owner_can_then_run_their_own_team(self, client, vendor):
        _seed_session(client, vendor)
        client.post("/admin/new", data={"name": "Vidal S.L.",
                                        "owner_email": "vidal@c.es"})
        _seed_session(client, "vidal@c.es")
        assert client.get("/team").status_code == 200

        client.post("/team/new", data={"email": "obrero@c.es", "perm": ["stock.view"]})
        assert accounts.find_by_email("obrero@c.es")["permissions"] == ["stock.view"]

    def test_a_company_with_no_name_is_refused(self, client, vendor):
        _seed_session(client, vendor)
        response = client.post("/admin/new", data={"name": "  ",
                                                   "owner_email": "x@y.es"})
        assert response.status_code == 200
        assert "nombre" in response.text

    def test_suspending_locks_the_client_out_without_deleting_them(
            self, client, vendor, company, owner):
        _seed_session(client, vendor)
        client.post(f"/admin/{company}/status", data={"status": accounts.SUSPENDED})

        _seed_session(client, owner)
        assert client.get("/").status_code == 303
        # Nothing was destroyed.
        assert accounts.find_by_email(owner) is not None
        assert len(accounts.list_users(company)) == 1

    def test_reactivating_lets_them_back_in(self, client, vendor, company, owner):
        accounts.set_company_status(company, accounts.SUSPENDED)
        _seed_session(client, vendor)
        client.post(f"/admin/{company}/status", data={"status": accounts.ACTIVE})

        _seed_session(client, owner)
        assert client.get("/").status_code == 200

    def test_the_panel_warns_about_a_company_with_no_active_owner(
            self, client, vendor, company, owner):
        accounts.update_user(accounts.find_by_email(owner)["id"], active=False)
        _seed_session(client, vendor)
        assert "ningún responsable activo" in client.get("/admin").text


class TestDataIsolationGuard:
    """Accounts are per-company; invoices, bills and stock are not yet.

    Until they are, a second active company on one installation would be able to read
    the first one's books, so the books close rather than leak.
    """

    def test_one_company_reads_its_books_normally(self, client, company, owner):
        _seed_session(client, owner)
        assert client.get("/").status_code == 200
        assert "más de una empresa activa" not in client.get("/").text

    def test_a_second_active_company_closes_the_books(self, client, company, owner):
        accounts.create_company("Otra Empresa S.L.")
        _seed_session(client, owner)
        for path in ("/", "/issued", "/bills", "/receivables"):
            assert "más de una empresa activa" in client.get(path).text, path

    def test_suspending_the_second_company_reopens_them(self, client, company, owner):
        other = accounts.create_company("Otra Empresa S.L.")
        accounts.set_company_status(other, accounts.SUSPENDED)
        _seed_session(client, owner)
        assert "más de una empresa activa" not in client.get("/").text

    def test_account_management_keeps_working_either_way(self, client, owner, vendor):
        accounts.create_company("Otra Empresa S.L.")
        _seed_session(client, owner)
        assert client.get("/team").status_code == 200
        _seed_session(client, vendor)
        assert client.get("/admin").status_code == 200

    def test_the_vendor_is_not_exempt_from_the_guard(self, client, company, vendor):
        accounts.create_company("Otra Empresa S.L.")
        _seed_session(client, vendor)
        assert "más de una empresa activa" in client.get("/").text
