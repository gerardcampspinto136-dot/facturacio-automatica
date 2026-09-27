"""Structured e-invoices: what FACe takes today and the B2B obligation will take.

The Facturae output is validated against the official 3.2.2 schema (facturae.gob.es)
and the UBL output against OASIS's UBL 2.1 schemas, both kept under tests/fixtures so
nothing here needs the network. On top of the schemas, the EN 16931 sums a receiving
system checks are checked too: an invoice can be well-formed and still not add up.
"""

from decimal import Decimal
from pathlib import Path

import pytest

from src import accounts, contacts, conversation, einvoice, finalize, rectify
from src.config_loader import reload_config
from src.models import InvoiceData, InvoiceItem

etree = pytest.importorskip("lxml.etree")

pytestmark = pytest.mark.usefixtures("offline")

FIXTURES = Path(__file__).parent / "fixtures"
UBL_NS = {"u": einvoice.UBL, "cac": einvoice.CAC, "cbc": einvoice.CBC}


@pytest.fixture(scope="module")
def facturae_schema():
    # The schema imports the signature schema from w3.org; point it at the local copy.
    path = FIXTURES / "facturae" / "Facturaev3_2_2.xsd"
    text = path.read_bytes().replace(
        b"http://www.w3.org/TR/xmldsig-core/xmldsig-core-schema.xsd",
        b"xmldsig-core-schema.xsd")
    return etree.XMLSchema(etree.fromstring(text, base_url=path.as_uri()))


@pytest.fixture(scope="module")
def ubl_schema():
    return etree.XMLSchema(etree.parse(str(FIXTURES / "ubl" / "maindoc" /
                                           "UBL-Invoice-2.1.xsd")))


def issue(**kw) -> str:
    return finalize.issue(InvoiceData(
        client_name=kw.pop("client_name", "Talleres Puig S.L."),
        client_email=kw.pop("client_email", "admin@tallerespuig.es"),
        client_id=kw.pop("client_id", "B12345678"),
        client_address=kw.pop("client_address", "Carrer Major 12, 17001 Girona"),
        items=kw.pop("items", [InvoiceItem("Reparación", 2, 150.0),
                               InvoiceItem("Piezas", 1, 80.5)]),
        **kw,
    )).number


def valid(schema, xml: bytes):
    document = etree.fromstring(xml)
    schema.assertValid(document)
    return document


def text(document, path: str, ns=None) -> str:
    return document.findtext(path, namespaces=ns)


@pytest.fixture
def professional():
    """A consultant: 15% IRPF withheld on every invoice."""
    company = accounts.create_company("Consultora Garcia", tax_id="12345678Z")
    accounts.update_company(company, irpf_rate=15)
    reload_config()


class TestFacturae:
    def test_a_normal_invoice_is_valid_facturae(self, facturae_schema):
        number = issue()
        doc = valid(facturae_schema, einvoice.facturae(number))

        assert text(doc, ".//InvoiceHeader/InvoiceNumber") == number
        assert text(doc, ".//InvoiceHeader/InvoiceClass") == "OO"
        totals = doc.find(".//InvoiceTotals")
        # 2 x 150 + 80.50 = 380.50; VAT 21% = 79.905 -> 79.91 (half up)
        assert (text(totals, "TotalGrossAmountBeforeTaxes"), text(totals, "TotalTaxOutputs"),
                text(totals, "InvoiceTotal")) == ("380.50", "79.91", "460.41")
        assert text(doc, ".//Batch/TotalInvoicesAmount/TotalAmount") == "460.41"

        seller, buyer = doc.find(".//SellerParty"), doc.find(".//BuyerParty")
        assert text(seller, "TaxIdentification/TaxIdentificationNumber") == "B00000000"
        assert text(seller, "LegalEntity/AddressInSpain/Province") == "Barcelona"
        assert text(buyer, "LegalEntity/CorporateName") == "Talleres Puig S.L."
        assert text(buyer, "LegalEntity/AddressInSpain/PostCode") == "17001"
        assert text(buyer, "LegalEntity/AddressInSpain/Town") == "Girona"
        # Transfer to the company's account, due when the invoice says.
        assert text(doc, ".//PaymentDetails/Installment/PaymentMeans") == "04"

    def test_only_the_root_is_namespaced(self):
        doc = etree.fromstring(einvoice.facturae(issue()))
        assert doc.tag == f"{{{einvoice.FE}}}Facturae"
        assert all(not child.tag.startswith("{") for child in doc.iter()
                   if child is not doc and isinstance(child.tag, str))

    def test_irpf_is_withheld(self, facturae_schema, professional):
        doc = valid(facturae_schema, einvoice.facturae(issue()))
        withheld = doc.find(".//Invoice/TaxesWithheld/Tax")
        assert (text(withheld, "TaxTypeCode"), text(withheld, "TaxRate")) == ("04", "15.00")
        assert text(withheld, "TaxAmount/TotalAmount") == "57.08"          # 15% of 380.50
        # What the client transfers: 380.50 + 79.91 - 57.08
        assert text(doc, ".//InvoiceTotals/InvoiceTotal") == "403.33"
        assert text(doc, ".//InvoiceTotals/TotalTaxesWithheld") == "57.08"

    def test_a_rectifying_invoice_points_at_the_original(self, facturae_schema):
        original = issue()
        credit = rectify.rectify(original).number
        doc = valid(facturae_schema, einvoice.facturae(credit))

        assert text(doc, ".//InvoiceHeader/InvoiceClass") == "OR"
        assert text(doc, ".//Corrective/InvoiceNumber") == original
        assert text(doc, ".//Corrective/CorrectionMethod") == "02"
        assert text(doc, ".//InvoiceTotals/InvoiceTotal") == "-460.41"
        assert doc.find(".//PaymentDetails") is None           # nothing to pay in

    def test_a_public_body_carries_its_dir3_codes(self, facturae_schema):
        town_hall = contacts.create(
            contacts.CLIENT, "Ajuntament de Girona", tax_id="P1708500B",
            email="factures@girona.cat", address="Plaça del Vi 1, 17004 Girona",
            dir3_accounting="L01170792", dir3_managing="L01170792",
            dir3_processing="LA0003421")
        number = issue(client_name="Ajuntament de Girona", client_id="P1708500B",
                       client_email="factures@girona.cat", contact_id=town_hall,
                       client_address="Plaça del Vi 1, 17004 Girona")
        doc = valid(facturae_schema, einvoice.facturae(number))

        centres = doc.findall(".//BuyerParty/AdministrativeCentres/AdministrativeCentre")
        assert [(text(c, "RoleTypeCode"), text(c, "CentreCode")) for c in centres] == [
            ("01", "L01170792"), ("02", "L01170792"), ("03", "LA0003421")]
        assert text(doc, ".//BuyerParty/TaxIdentification/PersonTypeCode") == "J"

    def test_two_dir3_codes_of_three_is_an_error(self):
        body = contacts.create(contacts.CLIENT, "Ajuntament de Salt", tax_id="P1716300I",
                               dir3_accounting="L01171634")
        number = issue(client_name="Ajuntament de Salt", client_id="P1716300I",
                       contact_id=body, client_address="Plaça Lluís Companys 1, 17190 Salt")
        with pytest.raises(einvoice.EInvoiceError, match="tres códigos DIR3"):
            einvoice.facturae(number)

    def test_a_self_employed_client_is_a_person(self, facturae_schema):
        number = issue(client_name="Marta Soler Puig", client_id="12345678Z",
                       client_address="C/ Nou 3, 2n, 17600 Figueres")
        doc = valid(facturae_schema, einvoice.facturae(number))
        person = doc.find(".//BuyerParty/Individual")
        assert (text(person, "Name"), text(person, "FirstSurname"),
                text(person, "SecondSurname")) == ("Marta", "Soler", "Puig")
        assert text(doc, ".//BuyerParty/TaxIdentification/PersonTypeCode") == "F"

    def test_a_vat_exempt_invoice(self, facturae_schema):
        number = issue(tax_rate=0, notes="Operación exenta de IVA (art. 20.Uno.9º LIVA)")
        doc = valid(facturae_schema, einvoice.facturae(number))
        assert text(doc, ".//InvoiceLine/SpecialTaxableEvent/SpecialTaxableEventCode") == "01"
        assert text(doc, ".//LegalLiterals/LegalReference").startswith("Operación exenta")

    def test_an_address_without_a_postcode_is_explained(self):
        number = issue(client_address="Girona")
        with pytest.raises(einvoice.EInvoiceError, match="código postal.*Clientes"):
            einvoice.facturae(number)

    def test_completing_the_client_record_is_enough(self, facturae_schema):
        # The issued invoice cannot change, but the file can take the full address
        # from the client's record once someone completes it.
        client = contacts.create(contacts.CLIENT, "Talleres Puig S.L.", tax_id="B12345678",
                                 address="Girona")
        number = issue(client_address="Girona", contact_id=client)
        with pytest.raises(einvoice.EInvoiceError):
            einvoice.facturae(number)
        contacts.update(client, address="Carrer Major 12, 17001 Girona")
        doc = valid(facturae_schema, einvoice.facturae(number))
        assert text(doc, ".//BuyerParty/LegalEntity/AddressInSpain/PostCode") == "17001"


class TestUBL:
    def test_a_normal_invoice_is_valid_ubl(self, ubl_schema):
        number = issue()
        doc = valid(ubl_schema, einvoice.ubl(number))
        assert text(doc, "cbc:CustomizationID", UBL_NS) == "urn:cen.eu:en16931:2017"
        assert text(doc, "cbc:ID", UBL_NS) == number
        assert text(doc, "cbc:InvoiceTypeCode", UBL_NS) == "380"
        seller = doc.find("cac:AccountingSupplierParty/cac:Party", UBL_NS)
        assert text(seller, "cac:PartyTaxScheme/cbc:CompanyID", UBL_NS) == "ESB00000000"
        assert text(doc, "cac:PaymentMeans/cac:PayeeFinancialAccount/cbc:ID", UBL_NS) \
            .startswith("ES")

    @pytest.mark.parametrize("withholding", [False, True])
    def test_the_sums_a_receiver_checks(self, ubl_schema, withholding, request):
        if withholding:
            request.getfixturevalue("professional")
        doc = valid(ubl_schema, einvoice.ubl(issue()))

        def amount(path, node=doc):
            value = node.findtext(path, namespaces=UBL_NS)
            return Decimal(value) if value is not None else Decimal("0")

        totals = doc.find("cac:LegalMonetaryTotal", UBL_NS)
        lines = doc.findall("cac:InvoiceLine", UBL_NS)
        # BR-CO-10: the lines add up to the line total
        assert sum(amount("cbc:LineExtensionAmount", l) for l in lines) == \
            amount("cbc:LineExtensionAmount", totals)
        # BR-CO-15: total with VAT = total without VAT + VAT
        assert amount("cbc:TaxInclusiveAmount", totals) == \
            amount("cbc:TaxExclusiveAmount", totals) + amount("cac:TaxTotal/cbc:TaxAmount")
        # BR-CO-16: due = total with VAT - already paid
        assert amount("cbc:PayableAmount", totals) == \
            amount("cbc:TaxInclusiveAmount", totals) - amount("cbc:PrepaidAmount", totals)
        # BR-S-09: the VAT is the rate applied to its base
        sub = doc.find("cac:TaxTotal/cac:TaxSubtotal", UBL_NS)
        assert amount("cbc:TaxAmount", sub) == (
            amount("cbc:TaxableAmount", sub) * amount("cac:TaxCategory/cbc:Percent", sub)
            / 100).quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
        if withholding:
            assert amount("cbc:PrepaidAmount", totals) == Decimal("57.08")
            assert amount("cac:WithholdingTaxTotal/cbc:TaxAmount") == Decimal("57.08")
            assert amount("cbc:PayableAmount", totals) == Decimal("403.33")

    def test_an_exempt_invoice_says_why_once(self, ubl_schema):
        # CEN's rules: the reason goes in the VAT breakdown, not on every line.
        doc = valid(ubl_schema, einvoice.ubl(issue(tax_rate=0)))
        category = doc.find("cac:TaxTotal/cac:TaxSubtotal/cac:TaxCategory", UBL_NS)
        assert text(category, "cbc:ID", UBL_NS) == "E"
        assert text(category, "cbc:TaxExemptionReason", UBL_NS)
        assert doc.find(".//cac:ClassifiedTaxCategory/cbc:TaxExemptionReason",
                        UBL_NS) is None

    def test_a_credit_has_negative_quantities_never_negative_prices(self, ubl_schema):
        original = issue()
        doc = valid(ubl_schema, einvoice.ubl(rectify.rectify(original).number))
        assert text(doc, "cbc:InvoiceTypeCode", UBL_NS) == "384"
        assert text(doc, "cac:BillingReference/cac:InvoiceDocumentReference/cbc:ID",
                    UBL_NS) == original
        for line in doc.findall("cac:InvoiceLine", UBL_NS):
            assert Decimal(text(line, "cac:Price/cbc:PriceAmount", UBL_NS)) >= 0   # BR-27
            assert Decimal(text(line, "cbc:InvoicedQuantity", UBL_NS)) < 0
        assert doc.find("cac:PaymentMeans", UBL_NS) is None


@pytest.fixture
def town_hall():
    return contacts.create(
        contacts.CLIENT, "Ajuntament de Girona", tax_id="P1708500B",
        email="factures@girona.cat", address="Plaça del Vi 1, 17004 Girona",
        dir3_accounting="L01170792", dir3_managing="L01170792",
        dir3_processing="LA0003421")


class TestSurfaces:
    @pytest.fixture
    def owner(self, monkeypatch):
        from test_telegram_access import OWNER_CHAT

        monkeypatch.setenv("TELEGRAM_CHAT_ID", str(OWNER_CHAT))
        reload_config()
        conversation._sessions.clear()
        return OWNER_CHAT

    def test_issuing_to_a_public_body_hands_over_the_face_file(self, town_hall, owner,
                                                               monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda _text: InvoiceData(
            client_name="Ajuntament de Girona", client_email="factures@girona.cat",
            client_id="P1708500B", items=[InvoiceItem("Taller de reciclaje", 1, 600.0)]))
        chat = say(bot.handle_text, owner, text="Factura para el Ajuntament de Girona")
        press(owner, "approve", chat)

        names = [name for name, _ in chat.documents]
        assert names[0].endswith(".pdf") and names[1].endswith("_Facturae.xml")
        assert "FACe" in chat.documents[1][1] and "AutoFirma" in chat.documents[1][1]

    def test_an_ordinary_client_gets_no_extra_file(self, owner, monkeypatch):
        from src import bot
        from test_telegram_access import press, say

        monkeypatch.setattr(bot, "parse_invoice_from_transcript", lambda _text: InvoiceData(
            client_name="Talleres Puig", client_email="taller@puig.es",
            client_id="B87654321", items=[InvoiceItem("Reparación", 1, 300.0)]))
        chat = say(bot.handle_text, owner, text="Factura para Talleres Puig")
        press(owner, "approve", chat)
        assert [name[-4:] for name, _ in chat.documents] == [".pdf"]

    def test_the_xml_command(self, owner):
        from src import bot
        from test_telegram_access import say

        number = issue()
        assert say(bot.cmd_xml, owner, number).documents[0][0] == \
            f"Factura_{number}_Facturae.xml"
        assert say(bot.cmd_xml, owner, number, "ubl").documents[0][0] == \
            f"Factura_{number}_UBL.xml"
        assert "No encuentro" in say(bot.cmd_xml, owner, "2099-9999").text
        assert "Uso" in say(bot.cmd_xml, owner).text

    def test_the_panel(self, town_hall, facturae_schema, monkeypatch):
        import base64
        import json

        import itsdangerous
        from fastapi.testclient import TestClient

        monkeypatch.delenv("WEB_DEV_NO_AUTH", raising=False)
        monkeypatch.setenv("SESSION_SECRET", "test-secret")
        from src.web import app as web_app

        company = accounts.create_company("Talleres Mario S.L.", tax_id="B12345678")
        accounts.create_user("jefe@talleres.es", accounts.ADMIN, company_id=company)
        client = TestClient(web_app.app, follow_redirects=False)
        signer = itsdangerous.TimestampSigner("test-secret")
        cookie = base64.b64encode(json.dumps({"user": "jefe@talleres.es"}).encode())
        client.cookies.set("session", signer.sign(cookie).decode())

        number = issue(client_name="Ajuntament de Girona", client_id="P1708500B",
                       contact_id=town_hall, client_address="Plaça del Vi 1, 17004 Girona")
        listing = client.get("/issued").text
        assert f"/issued/{number}/xml/facturae" in listing and "FACe ▾" in listing

        response = client.get(f"/issued/{number}/xml/facturae")
        assert response.status_code == 200
        assert f"Factura_{number}_Facturae.xml" in response.headers["content-disposition"]
        valid(facturae_schema, response.content)
        assert client.get(f"/issued/{number}/xml/ubl").status_code == 200
        assert "no existe" in client.get(f"/issued/{number}/xml/pdf").text

        # The DIR3 codes are kept on the client's record, cleaned up...
        page = client.get(f"/contacts/{town_hall}").text
        assert "Unidad tramitadora" in page and "LA0003421" in page
        client.post(f"/contacts/{town_hall}", data={
            "name": "Ajuntament de Girona", "tax_id": "P1708500B",
            "dir3_accounting": "l01 170 792", "dir3_managing": "L01170792",
            "dir3_processing": "LA0003421"})
        assert contacts.get(town_hall)["dir3_accounting"] == "L01170792"
        # ...and something that cannot be one is refused, not stored.
        refused = client.post(f"/contacts/{town_hall}", data={
            "name": "Ajuntament de Girona", "dir3_accounting": "ver web del ayuntamiento"})
        assert "no parece un código DIR3" in refused.text
        assert contacts.get(town_hall)["dir3_accounting"] == "L01170792"


class TestPieces:
    @pytest.mark.parametrize("address, expected", [
        ("Calle Mayor 1, 08001 Barcelona, España",
         ("Calle Mayor 1", "08001", "Barcelona", "Barcelona")),
        ("Av. Diagonal 640 08017 Barcelona", ("Av. Diagonal 640", "08017", "Barcelona",
                                              "Barcelona")),
        ("C/ Sol 4, 38001 Santa Cruz de Tenerife",
         ("C/ Sol 4", "38001", "Santa Cruz de Tenerife", "S.C. Tenerife")),
    ])
    def test_split_address(self, address, expected):
        place = einvoice.split_address(address)
        assert (place["address"], place["post_code"], place["town"],
                place["province"]) == expected

    @pytest.mark.parametrize("address", ["Barcelona", "", None, "Rue X, 75001 Paris"])
    def test_no_spanish_postcode(self, address):
        # 75001 would be "Paris" -- not a Spanish province prefix, so no guess is made.
        assert einvoice.split_address(address) is None

    @pytest.mark.parametrize("tax_id, where", [
        ("B12345678", "R"), ("12345678Z", "R"), ("X1234567L", "R"),
        ("FR12345678901", "U"), ("GB123456789", "E"),
    ])
    def test_residence(self, tax_id, where):
        assert einvoice.residence(tax_id) == where

    @pytest.mark.parametrize("full, parts", [
        ("Marta Soler Puig", ("Marta", "Soler", "Puig")),
        ("Juan Carlos García López", ("Juan Carlos", "García", "López")),
        ("María de la Fuente García", ("María", "de la Fuente", "García")),
        ("Pep Roca", ("Pep", "Roca", "")),
        ("Nadie", ("Nadie", "-", "")),
    ])
    def test_person_name(self, full, parts):
        assert einvoice.person_name(full) == parts

    def test_quantities_never_in_scientific_notation(self):
        assert einvoice._number(1_000_000) == "1000000"
        assert einvoice._number(2.5) == "2.5" and einvoice._number(1.0) == "1"
