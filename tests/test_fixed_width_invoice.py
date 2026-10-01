"""Dense monospaced exports retain columns, all pages and printed controls."""
from collections import Counter
from decimal import Decimal

import pymupdf
import pytest

from app.services import pdf_invoice, purchase_import, purchasing
from tests.test_purchasing import supplier


def fixed_invoice(*, count=204, printed_count=None, printed_units=None):
    doc = pymupdf.open()
    expected = []
    header = '  HSN      PRODUCT NAME                        PACK  MFG  BATCH           EXP      QTY  FR  M.R.P    RATE    AMOUNT  GST%'
    total = Decimal(0)
    received = Decimal(0)
    for start in range(0, count, 10):
        page = doc.new_page(width=650, height=842)
        lines = ['EXAMPLE DISTRIBUTOR', 'Invoice No: SYN-204  Date: 18/09/2026', '-' * 122, header, '-' * 122]
        for i in range(start, min(start + 10, count)):
            name = f'EXAMPLE PLUS  TABLET {i + 1}'
            paid, free = ('2.5', '0.5') if i == 6 else ('20.0', '')
            amt = Decimal(paid) * 2
            total += amt
            received += Decimal(paid) + Decimal(free or '0')
            fields = [(2, '30049099'), (11, name), (46, '15S'), (52, 'MAKR'), (57, f'B{i:09d}'),
                      (73, '10/2028'), (81, paid.rjust(5)), (87, free.rjust(4)),
                      (92, '125.00'.rjust(6)), (101, '2.00'.rjust(6)), (110, f'{amt:.2f}'.rjust(6)), (117, '5.0')]
            chars = [' '] * 122
            for at, text in fields:
                chars[at:at + len(text)] = text
            lines.append(''.join(chars))
            expected.append({'name':' '.join(name.split()),'quantity':paid,'free':free,'batch':f'B{i:09d}', 'expiry':'10/2028'})
        if start + 10 < count:
            lines.append('Continued on Next Page...')
        else:
            lines.extend(['-' * 122, f'No.of Items : {count if printed_count is None else printed_count}',
                          f'No.of Units : {received if printed_units is None else printed_units}',
                          f'SubTotal: {total:.2f}', f'GST Amt: {total * Decimal(".05"):.2f}',
                          'Rounding: 0.00', f'NET AMOUNT: {total * Decimal("1.05"):.2f}'])
        for n, line in enumerate(lines):
            page.insert_text((6, 30 + n * 12), line, fontname='cour', fontsize=8)
    data = doc.tobytes()
    doc.close()
    return data, expected


def test_all_21_pages_and_adjacent_fields_survive():
    data, expected = fixed_invoice()
    doc = purchase_import.parse('dense.pdf', data)
    assert doc.method == 'fixed_width'
    assert len(doc.lines) == 204
    assert Counter(l.page for l in doc.lines) == {**{p:10 for p in range(1,21)},21:4}
    for line, want in zip(doc.lines, expected):
        for key, value in want.items():
            assert line.raw[key] == value
        assert line.raw['hsn'] == '30049099'
        assert line.raw['pack'] == '15S'
        assert line.raw['manufacturer'] == 'MAKR'
    assert doc.charges['_expected_lines'] == '204'
    assert not doc.charges.get('_extraction_issues')
    assert doc.lines[6].raw['free'] == '0.5'


@pytest.mark.parametrize('kwargs', [{'printed_count':205}, {'printed_units':9999}])
def test_incomplete_pdf_controls_block_posting_even_when_difference_is_accepted(db, kwargs):
    data, _ = fixed_invoice(count=12, **kwargs)
    p = purchasing.create_from_file(db,'dense.pdf',data,supplier_id=supplier(db).id)
    assert p.charges['_extraction_issues']
    with pytest.raises(purchasing.PurchaseError, match='extraction'):
        purchasing.post(db,p,accept_difference=True)


def test_fixed_width_reader_does_not_claim_proportional_text():
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((20,40),'Product Name    Qty    Rate    MRP', fontname='helv')
    page.insert_text((20,60),'Medicine        2      10      20', fontname='helv')
    assert pdf_invoice.fixed_width(doc) is None
