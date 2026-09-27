"""Structured electronic invoices: Facturae (FACe, public bodies) and UBL (B2B).

Two obligations, one invoice:

  FACe      Invoices to Spanish public bodies (town halls, ministries, universities...)
            go through FACe as Facturae 3.2.x XML, signed, addressed with the body's
            three DIR3 codes (accounting office, managing body, processing unit).
  B2B       RD 238/2026 makes electronic invoicing between businesses compulsory --
            October 2027 above 8 M EUR of turnover, October 2028 for everyone else --
            in a syntax of the EN 16931 model (UBL, CII, EDIFACT or Facturae); the
            AEAT's free public solution takes UBL.

Both are built here from the issued invoice record. The tests check the Facturae
output against the official 3.2.2 schema (facturae.gob.es) and the UBL output against
OASIS's UBL 2.1 schemas; the UBL also passed CEN's own EN 16931 business rules (the
validation artefacts, v1.3.16) with no errors. Clients outside Spain are not covered:
both obligations are about Spanish addresses.

Not done here: the XAdES signature FACe requires. Until a company's certificate is
wired in, the XML is signed with AutoFirma -- the government's free signing app -- in
one drag and drop, and then uploaded to FACe.
"""

import re
import xml.etree.ElementTree as ET
from typing import Optional

from src.config_loader import get_config
from src.models import InvoiceData, percent_of

FE = "http://www.facturae.gob.es/formato/Versiones/Facturaev3_2_2.xml"
UBL = "urn:oasis:names:specification:ubl:schema:xsd:Invoice-2"
CAC = "urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2"
CBC = "urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2"

# Postcode prefix -> province, as Facturae wants it (at most 20 characters).
PROVINCES = {
    "01": "Álava", "02": "Albacete", "03": "Alicante", "04": "Almería", "05": "Ávila",
    "06": "Badajoz", "07": "Illes Balears", "08": "Barcelona", "09": "Burgos",
    "10": "Cáceres", "11": "Cádiz", "12": "Castellón", "13": "Ciudad Real",
    "14": "Córdoba", "15": "A Coruña", "16": "Cuenca", "17": "Girona", "18": "Granada",
    "19": "Guadalajara", "20": "Gipuzkoa", "21": "Huelva", "22": "Huesca", "23": "Jaén",
    "24": "León", "25": "Lleida", "26": "La Rioja", "27": "Lugo", "28": "Madrid",
    "29": "Málaga", "30": "Murcia", "31": "Navarra", "32": "Ourense", "33": "Asturias",
    "34": "Palencia", "35": "Las Palmas", "36": "Pontevedra", "37": "Salamanca",
    "38": "S.C. Tenerife", "39": "Cantabria", "40": "Segovia", "41": "Sevilla",
    "42": "Soria", "43": "Tarragona", "44": "Teruel", "45": "Toledo", "46": "Valencia",
    "47": "Valladolid", "48": "Bizkaia", "49": "Zamora", "50": "Zaragoza",
    "51": "Ceuta", "52": "Melilla",
}

EU_PREFIXES = {"AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "EL", "FI", "FR", "HR",
               "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE",
               "SI", "SK"}


class EInvoiceError(Exception):
    """Missing data a structured invoice cannot do without, in words."""


# ── What both formats need ───────────────────────────────────────────────────

def split_address(text: Optional[str]) -> Optional[dict]:
    """"Calle Mayor 1, 08001 Barcelona, España" -> street, postcode, town, province.

    None when there is no Spanish postcode to anchor on.
    """
    text = (text or "").strip()
    match = re.search(r"\b(\d{5})\b\s*([^,]*)", text)
    if not match or match.group(1)[:2] not in PROVINCES:
        return None
    post_code = match.group(1)
    street = text[:match.start()].strip(" ,") or text
    town = match.group(2).strip(" ,") or PROVINCES[post_code[:2]]
    return {"address": street[:80], "post_code": post_code, "town": town[:50],
            "province": PROVINCES[post_code[:2]][:20]}


def clean_tax_id(value: Optional[str]) -> str:
    return re.sub(r"[\s.\-]", "", value or "").upper()


def is_legal_person(tax_id: str) -> bool:
    """A company's tax id starts with the letter of its legal form; a person's doesn't."""
    return bool(tax_id) and tax_id[0] in "ABCDEFGHJNPQRSUVW"


def residence(tax_id: str) -> str:
    """R: resident in Spain; U: elsewhere in the EU; E: outside the EU."""
    if tax_id[:2] in EU_PREFIXES:
        return "U"
    if re.fullmatch(r"[A-Z]?\d{7,8}[A-Z0-9]?|[XYZKLM]\d{7}[A-Z]", tax_id):
        return "R"
    return "E"


_PARTICLES = {"de", "del", "la", "las", "los", "y", "i", "san", "da", "dos"}


def person_name(full: str) -> tuple[str, str, str]:
    """"Juan Carlos García López" -> ("Juan Carlos", "García", "López").

    Spanish names end in two surnames, so those are the last two words -- with "de la
    Fuente" kept as one surname. A single word is left as the name, surname "-".
    """
    parts: list[str] = []
    pending: list[str] = []
    for word in full.split():
        if word.lower() in _PARTICLES and parts:
            pending.append(word)
            continue
        parts.append(" ".join(pending + [word]))
        pending = []
    if pending:
        parts[-1] = " ".join([parts[-1], *pending])
    if len(parts) >= 3:
        return " ".join(parts[:-2]), parts[-2], parts[-1]
    if len(parts) == 2:
        return parts[0], parts[1], ""
    return (parts[0] if parts else full.strip() or "-"), "-", ""


def _party_data(name: str, tax_id: Optional[str], address: Optional[str],
                where: str) -> dict:
    tax_id = clean_tax_id(tax_id)
    if not tax_id or tax_id == "SINNIF":
        raise EInvoiceError(f"Falta el NIF de «{name}»: una factura electrónica lo exige. "
                            f"{where}")
    place = split_address(address)
    if place is None:
        raise EInvoiceError(
            f"La dirección de «{name}» necesita un código postal español para la factura "
            f"electrónica (ahora es: «{address or 'vacía'}»). {where}")
    return {"name": name, "tax_id": tax_id, "place": place}


def _seller() -> dict:
    config = get_config()
    return _party_data(config.name, config.cif, config.address,
                       "Corrígelo en los datos de la empresa, en el panel.")


def _buyer(invoice: InvoiceData) -> dict:
    """The client as on the invoice -- with the address from their record if the
    invoice's lacks the postcode, so completing the record is enough to fix it."""
    from src import contacts

    address = invoice.client_address
    if split_address(address) is None and invoice.contact_id:
        address = (contacts.get(invoice.contact_id) or {}).get("address") or address
    return _party_data(invoice.client_name, invoice.client_id, address,
                       "Complétalo en su ficha de Clientes y vuelve a pedirla.")


def _dir3(invoice: InvoiceData) -> Optional[list[tuple[str, str]]]:
    """The public body's DIR3 codes, from the client record, if it has any."""
    from src import contacts

    if not invoice.contact_id:
        return None
    contact = contacts.get(invoice.contact_id) or {}
    codes = [("01", contact.get("dir3_accounting")), ("02", contact.get("dir3_managing")),
             ("03", contact.get("dir3_processing"))]
    if not any(code for _, code in codes):
        return None
    if not all(code for _, code in codes):
        raise EInvoiceError("Para FACe hacen falta los tres códigos DIR3 del cliente "
                            "(oficina contable, órgano gestor y unidad tramitadora).")
    return [(role, code.strip().upper()) for role, code in codes]


def is_public_body(invoice: InvoiceData) -> bool:
    """Whether the client is a public administration, i.e. invoiced through FACe."""
    from src import contacts

    contact = contacts.get(invoice.contact_id) if invoice.contact_id else None
    return bool(contact) and any(contact.get(key) for key in
                                 ("dir3_accounting", "dir3_managing", "dir3_processing"))


def build(number: str, kind: str = "facturae") -> tuple[bytes, str]:
    """(xml, file name) for `kind` "facturae" or "ubl"."""
    if kind == "ubl":
        return ubl(number), f"Factura_{number}_UBL.xml"
    return facturae(number), f"Factura_{number}_Facturae.xml"


FACE_STEPS = ("Para presentarla en FACe:\n"
              "1. Ábrela con AutoFirma y pulsa «Firmar» (sale un archivo .xsig).\n"
              "2. En face.gob.es, «Remitir factura», sube ese .xsig.")


def _load(number: str):
    from src import store
    from src.totals import breakdown

    record = store.get_issued(number)
    if record is None:
        raise EInvoiceError(f"No encuentro la factura {number}.")
    invoice: InvoiceData = record["invoice"]
    return record, invoice, breakdown(invoice, get_config())


def _money(value: float) -> str:
    return f"{value:.2f}"


def _number(value: float) -> str:
    """1.0 -> "1", 2.5 -> "2.5", 1000000 -> "1000000" (never "1e+06")."""
    return f"{value:.8f}".rstrip("0").rstrip(".") or "0"


def _el(parent, tag: str, text=None, **attrs):
    element = ET.SubElement(parent, tag, {k: str(v) for k, v in attrs.items()})
    if text is not None:
        element.text = str(text)
    return element


# ── Facturae 3.2.2 ───────────────────────────────────────────────────────────
# Only the root element is in the Facturae namespace (the schema's elements are
# unqualified), so everything below it is built without one.

def _fe_address(parent, place: dict) -> None:
    box = _el(parent, "AddressInSpain")
    _el(box, "Address", place["address"])
    _el(box, "PostCode", place["post_code"])
    _el(box, "Town", place["town"])
    _el(box, "Province", place["province"])
    _el(box, "CountryCode", "ESP")


def _fe_party(parent, tag: str, data: dict, email: Optional[str] = None,
              centres: Optional[list] = None) -> None:
    party = _el(parent, tag)
    ident = _el(party, "TaxIdentification")
    legal = is_legal_person(data["tax_id"])
    _el(ident, "PersonTypeCode", "J" if legal else "F")
    _el(ident, "ResidenceTypeCode", residence(data["tax_id"]))
    _el(ident, "TaxIdentificationNumber", data["tax_id"])
    if centres:
        box = _el(party, "AdministrativeCentres")
        for role, code in centres:
            centre = _el(box, "AdministrativeCentre")
            _el(centre, "CentreCode", code)
            _el(centre, "RoleTypeCode", role)
            _fe_address(centre, data["place"])
    if legal:
        holder = _el(party, "LegalEntity")
        _el(holder, "CorporateName", data["name"][:80])
    else:
        holder = _el(party, "Individual")
        name, first_surname, second_surname = person_name(data["name"])
        _el(holder, "Name", name[:40])
        _el(holder, "FirstSurname", first_surname[:40])
        if second_surname:
            _el(holder, "SecondSurname", second_surname[:40])
    _fe_address(holder, data["place"])
    if email:
        _el(_el(holder, "ContactDetails"), "ElectronicMail", email[:60])


def _fe_tax(parent, code: str, rate: float, base: float, amount: float) -> None:
    tax = _el(parent, "Tax")
    _el(tax, "TaxTypeCode", code)
    _el(tax, "TaxRate", f"{rate:.2f}")
    _el(_el(tax, "TaxableBase"), "TotalAmount", _money(base))
    _el(_el(tax, "TaxAmount"), "TotalAmount", _money(amount))


def facturae(number: str) -> bytes:
    """The issued invoice `number` as a Facturae 3.2.2 document (unsigned)."""
    from src import store

    record, invoice, t = _load(number)
    config = get_config()
    seller = _seller()
    buyer = _buyer(invoice)

    ET.register_namespace("fe", FE)
    root = ET.Element(f"{{{FE}}}Facturae")

    header = _el(root, "FileHeader")
    _el(header, "SchemaVersion", "3.2.2")
    _el(header, "Modality", "I")
    _el(header, "InvoiceIssuerType", "EM")
    batch = _el(header, "Batch")
    _el(batch, "BatchIdentifier", f"{seller['tax_id']}{number}"[:70])
    _el(batch, "InvoicesCount", 1)
    for tag in ("TotalInvoicesAmount", "TotalOutstandingAmount", "TotalExecutableAmount"):
        _el(_el(batch, tag), "TotalAmount", _money(t.total))
    _el(batch, "InvoiceCurrencyCode", "EUR")

    parties = _el(root, "Parties")
    _fe_party(parties, "SellerParty", seller, config.email)
    _fe_party(parties, "BuyerParty", buyer, invoice.client_email, _dir3(invoice))

    doc = _el(_el(root, "Invoices"), "Invoice")
    head = _el(doc, "InvoiceHeader")
    _el(head, "InvoiceNumber", number[:20])
    _el(head, "InvoiceDocumentType", "FC")
    _el(head, "InvoiceClass", "OR" if invoice.rectifies else "OO")
    if invoice.rectifies:
        original = store.get_issued(invoice.rectifies)
        when = (original["invoice"].date if original else invoice.date).isoformat()
        corrective = _el(head, "Corrective")
        _el(corrective, "InvoiceNumber", invoice.rectifies[:20])
        _el(corrective, "ReasonCode", "16")
        _el(corrective, "ReasonDescription", "Base imponible")
        period = _el(corrective, "TaxPeriod")
        _el(period, "StartDate", when)
        _el(period, "EndDate", when)
        # The rectifying invoice shows the difference (the original, negated).
        _el(corrective, "CorrectionMethod", "02")
        _el(corrective, "CorrectionMethodDescription", "Rectificación por diferencias")

    issue = _el(doc, "InvoiceIssueData")
    _el(issue, "IssueDate", invoice.date.isoformat())
    _el(issue, "InvoiceCurrencyCode", "EUR")
    _el(issue, "TaxCurrencyCode", "EUR")
    _el(issue, "LanguageName", "es")

    _fe_tax(_el(doc, "TaxesOutputs"), "01", t.tax_rate, t.base, t.tax)
    if t.irpf:
        _fe_tax(_el(doc, "TaxesWithheld"), "04", t.irpf_rate, t.base, t.irpf)

    totals = _el(doc, "InvoiceTotals")
    _el(totals, "TotalGrossAmount", _money(t.base))
    _el(totals, "TotalGrossAmountBeforeTaxes", _money(t.base))
    _el(totals, "TotalTaxOutputs", _money(t.tax))
    _el(totals, "TotalTaxesWithheld", _money(t.irpf))
    _el(totals, "InvoiceTotal", _money(t.total))
    _el(totals, "TotalOutstandingAmount", _money(t.total))
    _el(totals, "TotalExecutableAmount", _money(t.total))

    items = _el(doc, "Items")
    for item in invoice.items:
        line = _el(items, "InvoiceLine")
        _el(line, "ItemDescription", (item.description or "Servicio")[:2500])
        _el(line, "Quantity", _number(item.quantity))
        _el(line, "UnitOfMeasure", "01")
        _el(line, "UnitPriceWithoutTax", f"{item.unit_price:.6f}")
        _el(line, "TotalCost", _money(item.total))
        _el(line, "GrossAmount", _money(item.total))
        _fe_tax(_el(line, "TaxesOutputs"), "01", t.tax_rate, item.total,
                percent_of(item.total, t.tax_rate))
        if not t.tax_rate:
            event = _el(line, "SpecialTaxableEvent")
            _el(event, "SpecialTaxableEventCode", "01")
            _el(event, "SpecialTaxableEventReason", "Operación exenta de IVA")

    if config.bank_account and not invoice.rectifies and t.total > 0:
        installment = _el(_el(doc, "PaymentDetails"), "Installment")
        _el(installment, "InstallmentDueDate",
            (invoice.due_date or invoice.date).isoformat())
        _el(installment, "InstallmentAmount", _money(t.total))
        _el(installment, "PaymentMeans", "04")                 # transfer
        _el(_el(installment, "AccountToBeCredited"), "IBAN",
            re.sub(r"\s", "", config.bank_account))

    if invoice.notes:
        _el(_el(doc, "LegalLiterals"), "LegalReference", invoice.notes[:250])

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, "utf-8")


# ── UBL 2.1 (EN 16931) ───────────────────────────────────────────────────────

def _cbc(parent, tag, text=None, **attrs):
    return _el(parent, f"{{{CBC}}}{tag}", text, **attrs)


def _cac(parent, tag):
    return _el(parent, f"{{{CAC}}}{tag}")


def _ubl_party(parent, tag: str, data: dict, email: Optional[str]) -> None:
    party = _cac(_cac(parent, tag), "Party")
    place = data["place"]
    address = _cac(party, "PostalAddress")
    _cbc(address, "StreetName", place["address"])
    _cbc(address, "CityName", place["town"])
    _cbc(address, "PostalZone", place["post_code"])
    _cbc(address, "CountrySubentity", place["province"])
    _cbc(_cac(address, "Country"), "IdentificationCode", "ES")
    scheme = _cac(party, "PartyTaxScheme")
    vat_id = data["tax_id"] if data["tax_id"][:2] in EU_PREFIXES else f"ES{data['tax_id']}"
    _cbc(scheme, "CompanyID", vat_id)
    _cbc(_cac(scheme, "TaxScheme"), "ID", "VAT")
    legal = _cac(party, "PartyLegalEntity")
    _cbc(legal, "RegistrationName", data["name"])
    _cbc(legal, "CompanyID", data["tax_id"])
    if email:
        _cbc(_cac(party, "Contact"), "ElectronicMail", email)


def _ubl_category(parent, tag: str, rate: float, reason: bool = False) -> None:
    category = _cac(parent, tag)
    _cbc(category, "ID", "S" if rate else "E")
    _cbc(category, "Percent", _number(rate))
    # The exemption reason belongs in the VAT breakdown only, not on each line.
    if reason and not rate:
        _cbc(category, "TaxExemptionReason", "Operación exenta de IVA")
    _cbc(_cac(category, "TaxScheme"), "ID", "VAT")


def ubl(number: str) -> bytes:
    """The issued invoice `number` as a UBL 2.1 invoice following EN 16931.

    IRPF withholding has no place in EN 16931: the invoice total with VAT is what the
    VAT is declared on, and the client transfers that minus the IRPF. So the withheld
    amount goes in as already settled (PrepaidAmount -- the client pays it to Hacienda
    on the supplier's account), which keeps the standard's own arithmetic exact, and in
    UBL's WithholdingTaxTotal, which says what it is. The Spanish order implementing
    RD 238/2026 may settle it differently; this is the one place to change.
    """
    record, invoice, t = _load(number)
    config = get_config()
    seller = _seller()
    buyer = _buyer(invoice)

    ET.register_namespace("", UBL)
    ET.register_namespace("cac", CAC)
    ET.register_namespace("cbc", CBC)
    root = ET.Element(f"{{{UBL}}}Invoice")
    _cbc(root, "CustomizationID", "urn:cen.eu:en16931:2017")
    _cbc(root, "ID", number)
    _cbc(root, "IssueDate", invoice.date.isoformat())
    if invoice.due_date and t.total > 0:
        _cbc(root, "DueDate", invoice.due_date.isoformat())
    # 384: a corrected invoice, pointing at the one it corrects.
    _cbc(root, "InvoiceTypeCode", "384" if invoice.rectifies else "380")
    if invoice.notes:
        _cbc(root, "Note", invoice.notes)
    _cbc(root, "DocumentCurrencyCode", "EUR")
    if invoice.rectifies:
        reference = _cac(_cac(root, "BillingReference"), "InvoiceDocumentReference")
        _cbc(reference, "ID", invoice.rectifies)

    _ubl_party(root, "AccountingSupplierParty", seller, config.email)
    _ubl_party(root, "AccountingCustomerParty", buyer, invoice.client_email)

    if t.total > 0:
        if config.bank_account:
            means = _cac(root, "PaymentMeans")
            _cbc(means, "PaymentMeansCode", "58")              # SEPA credit transfer
            _cbc(means, "PaymentID", number)
            _cbc(_cac(means, "PayeeFinancialAccount"), "ID",
                 re.sub(r"\s", "", config.bank_account))
        _cbc(_cac(root, "PaymentTerms"), "Note", config.payment_terms)

    tax_total = _cac(root, "TaxTotal")
    _cbc(tax_total, "TaxAmount", _money(t.tax), currencyID="EUR")
    subtotal = _cac(tax_total, "TaxSubtotal")
    _cbc(subtotal, "TaxableAmount", _money(t.base), currencyID="EUR")
    _cbc(subtotal, "TaxAmount", _money(t.tax), currencyID="EUR")
    _ubl_category(subtotal, "TaxCategory", t.tax_rate, reason=True)

    if t.irpf:
        withholding = _cac(root, "WithholdingTaxTotal")
        _cbc(withholding, "TaxAmount", _money(t.irpf), currencyID="EUR")
        sub = _cac(withholding, "TaxSubtotal")
        _cbc(sub, "TaxableAmount", _money(t.base), currencyID="EUR")
        _cbc(sub, "TaxAmount", _money(t.irpf), currencyID="EUR")
        category = _cac(sub, "TaxCategory")
        _cbc(category, "Percent", _number(t.irpf_rate))
        _cbc(_cac(category, "TaxScheme"), "ID", "IRPF")

    totals = _cac(root, "LegalMonetaryTotal")
    _cbc(totals, "LineExtensionAmount", _money(t.base), currencyID="EUR")
    _cbc(totals, "TaxExclusiveAmount", _money(t.base), currencyID="EUR")
    _cbc(totals, "TaxInclusiveAmount", _money(t.gross), currencyID="EUR")
    if t.irpf:
        _cbc(totals, "PrepaidAmount", _money(t.irpf), currencyID="EUR")
    _cbc(totals, "PayableAmount", _money(t.total), currencyID="EUR")

    for index, item in enumerate(invoice.items, 1):
        # A price is never negative in EN 16931: a credit line is a negative quantity.
        sign = -1 if item.unit_price < 0 else 1
        line = _cac(root, "InvoiceLine")
        _cbc(line, "ID", index)
        _cbc(line, "InvoicedQuantity", _number(sign * item.quantity), unitCode="C62")
        _cbc(line, "LineExtensionAmount", _money(item.total), currencyID="EUR")
        product = _cac(line, "Item")
        _cbc(product, "Name", (item.description or "Servicio")[:200])
        _ubl_category(product, "ClassifiedTaxCategory", t.tax_rate)
        _cbc(_cac(line, "Price"), "PriceAmount", _number(sign * item.unit_price),
             currencyID="EUR")

    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, "utf-8")
