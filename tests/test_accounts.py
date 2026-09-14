"""Companies, accounts, and who is allowed to do what.

These are the rules that decide whether a stranger can approve an invoice, so they are
tested directly rather than through the web app.
"""

import pytest

from src import accounts


@pytest.fixture
def company():
    return accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")


@pytest.fixture
def owner(company):
    return accounts.load_context(
        accounts.create_user("mario@talleres.es", accounts.ADMIN, company_id=company)
    )


@pytest.fixture
def vendor():
    return accounts.load_context(
        accounts.create_user("gerard@vendor.es", accounts.SUPERADMIN)
    )


def employee_of(company, email="pepe@talleres.es", permissions=None):
    return accounts.load_context(accounts.create_user(
        email, accounts.EMPLOYEE, company_id=company, permissions=permissions
    ))


# ── Companies ────────────────────────────────────────────────────────────────

class TestCompanies:
    def test_created_active_by_default(self, company):
        assert accounts.get_company(company)["status"] == accounts.ACTIVE

    def test_a_company_needs_a_name(self):
        with pytest.raises(ValueError):
            accounts.create_company("   ")

    def test_suspending_keeps_the_records(self, company, owner):
        accounts.set_company_status(company, accounts.SUSPENDED)
        assert accounts.get_company(company)["status"] == accounts.SUSPENDED
        # The accounts are still there, they just cannot be used.
        assert len(accounts.list_users(company)) == 1

    def test_an_unknown_status_is_refused(self, company):
        with pytest.raises(ValueError):
            accounts.set_company_status(company, "deleted")


# ── Creating accounts ────────────────────────────────────────────────────────

class TestCreatingAccounts:
    def test_an_employee_gets_a_read_mostly_starting_set(self, company):
        user = employee_of(company)
        assert "invoices.view" in user["permissions"]
        # Approving and sending money out is never granted by default.
        assert "invoices.approve" not in user["permissions"]
        assert "users.manage" not in user["permissions"]

    def test_emails_are_stored_lowercased(self, company):
        accounts.create_user("Pepe@Talleres.ES", accounts.EMPLOYEE, company_id=company)
        assert accounts.find_by_email("pepe@talleres.es") is not None
        assert accounts.find_by_email("PEPE@TALLERES.ES") is not None

    def test_the_same_email_cannot_be_added_twice(self, company):
        import sqlite3

        accounts.create_user("pepe@talleres.es", accounts.EMPLOYEE, company_id=company)
        with pytest.raises(sqlite3.IntegrityError):
            accounts.create_user("PEPE@talleres.es", accounts.EMPLOYEE,
                                 company_id=company)

    def test_an_invalid_email_is_refused(self, company):
        with pytest.raises(ValueError):
            accounts.create_user("no-es-un-email", accounts.EMPLOYEE, company_id=company)

    def test_an_employee_must_belong_to_a_company(self):
        with pytest.raises(ValueError):
            accounts.create_user("suelto@x.es", accounts.EMPLOYEE)

    def test_a_superadmin_belongs_to_no_company(self, company):
        with pytest.raises(ValueError):
            accounts.create_user("v@x.es", accounts.SUPERADMIN, company_id=company)

    def test_an_unknown_role_is_refused(self, company):
        with pytest.raises(ValueError):
            accounts.create_user("x@y.es", "dios", company_id=company)

    def test_made_up_permissions_are_not_stored(self, company):
        user = employee_of(company, permissions=["invoices.view", "borrar.todo"])
        assert user["permissions"] == ["invoices.view"]


# ── The rules ────────────────────────────────────────────────────────────────

class TestPermissions:
    def test_an_owner_can_do_everything_in_their_company(self, owner):
        for key in accounts.PERMISSIONS:
            assert accounts.can(owner, key), key

    def test_an_owner_cannot_manage_other_companies(self, owner):
        assert accounts.can(owner, "companies.manage") is False

    def test_the_vendor_can_do_everything(self, vendor):
        assert accounts.can(vendor, "companies.manage")
        assert accounts.can(vendor, "invoices.approve")

    def test_an_employee_only_gets_what_was_granted(self, company):
        user = employee_of(company, permissions=["invoices.view", "bills.view"])
        assert accounts.can(user, "invoices.view")
        assert accounts.can(user, "bills.view")
        assert accounts.can(user, "invoices.approve") is False
        assert accounts.can(user, "bills.manage") is False

    def test_granting_approval_works(self, company):
        user = employee_of(company, permissions=["invoices.approve"])
        assert accounts.can(user, "invoices.approve")

    def test_a_deactivated_account_can_do_nothing(self, company):
        user = employee_of(company, permissions=list(accounts.PERMISSIONS))
        accounts.update_user(user["id"], active=False)
        assert accounts.can(accounts.load_context(user["id"]), "invoices.view") is False

    def test_a_suspended_company_freezes_its_owner_too(self, company, owner):
        accounts.set_company_status(company, accounts.SUSPENDED)
        frozen = accounts.load_context(owner["id"])
        assert accounts.can(frozen, "invoices.view") is False
        assert accounts.effective_permissions(frozen) == set()

    def test_a_suspended_company_does_not_freeze_the_vendor(self, company, vendor):
        accounts.set_company_status(company, accounts.SUSPENDED)
        assert accounts.can(vendor, "companies.manage")

    def test_nobody_is_not_allowed_anything(self):
        assert accounts.can(None, "invoices.view") is False
        assert accounts.effective_permissions(None) == set()


# ── Who may administer whom ──────────────────────────────────────────────────

class TestManagingEachOther:
    def test_an_owner_manages_their_own_staff(self, company, owner):
        assert accounts.may_manage(owner, employee_of(company))

    def test_an_owner_cannot_touch_another_companys_staff(self, owner):
        other = accounts.create_company("Otra Empresa S.L.")
        assert accounts.may_manage(owner, employee_of(other, "ajeno@otra.es")) is False

    def test_an_owner_cannot_touch_the_vendor(self, owner, vendor):
        assert accounts.may_manage(owner, vendor) is False

    def test_the_vendor_manages_anyone(self, company, vendor, owner):
        assert accounts.may_manage(vendor, owner)
        assert accounts.may_manage(vendor, employee_of(company))

    def test_nobody_edits_their_own_account(self, owner, vendor):
        # This is how someone removes their own admin rights and locks everyone out.
        assert accounts.may_manage(owner, owner) is False
        assert accounts.may_manage(vendor, vendor) is False

    def test_an_employee_cannot_manage_anyone(self, company, owner):
        pepe = employee_of(company)
        assert accounts.may_manage(pepe, owner) is False

    def test_an_employee_granted_users_manage_can(self, company):
        boss = employee_of(company, "jefe@talleres.es", permissions=["users.manage"])
        assert accounts.may_manage(boss, employee_of(company))

    def test_the_last_admin_is_countable_so_callers_can_refuse(self, company, owner):
        assert accounts.count_active_admins(company) == 1
        assert accounts.count_active_admins(company, excluding=owner["id"]) == 0


# ── Signing in ───────────────────────────────────────────────────────────────

class TestAuthenticate:
    def test_a_known_user_is_let_in(self, owner):
        user, refusal = accounts.authenticate("mario@talleres.es")
        assert refusal is None
        assert user["id"] == owner["id"]
        assert user["company_name"] == "Talleres Mario S.L."

    def test_login_is_recorded(self, owner):
        accounts.authenticate("mario@talleres.es")
        assert accounts.get_user(owner["id"])["last_login_at"] is not None

    def test_case_does_not_matter(self, owner):
        user, refusal = accounts.authenticate("Mario@Talleres.ES")
        assert refusal is None and user is not None

    def test_a_stranger_is_told_what_to_do(self):
        user, refusal = accounts.authenticate("cualquiera@internet.com")
        assert user is None
        assert "no tiene acceso" in refusal

    def test_a_deactivated_account_is_refused(self, company):
        pepe = employee_of(company)
        accounts.update_user(pepe["id"], active=False)
        user, refusal = accounts.authenticate("pepe@talleres.es")
        assert user is None
        assert "desactivada" in refusal

    def test_a_suspended_company_is_refused_by_name(self, company, owner):
        accounts.set_company_status(company, accounts.SUSPENDED)
        user, refusal = accounts.authenticate("mario@talleres.es")
        assert user is None
        assert "Talleres Mario S.L." in refusal
        assert "suspendido" in refusal

    def test_the_first_vendor_login_bootstraps_an_account(self, monkeypatch):
        monkeypatch.setenv("SUPERADMIN_EMAILS", "gerard@vendor.es, otro@vendor.es")
        user, refusal = accounts.authenticate("gerard@vendor.es")
        assert refusal is None
        assert user["role"] == accounts.SUPERADMIN
        assert user["company_id"] is None

    def test_bootstrapping_happens_only_for_listed_addresses(self, monkeypatch):
        monkeypatch.setenv("SUPERADMIN_EMAILS", "gerard@vendor.es")
        user, refusal = accounts.authenticate("impostor@vendor.es")
        assert user is None and refusal

    def test_bootstrapping_does_not_duplicate_the_account(self, monkeypatch):
        monkeypatch.setenv("SUPERADMIN_EMAILS", "gerard@vendor.es")
        accounts.authenticate("gerard@vendor.es")
        accounts.authenticate("gerard@vendor.es")
        assert len(accounts.list_users()) == 1
