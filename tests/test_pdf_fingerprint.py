from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject, DictionaryObject, NameObject, NumberObject, StreamObject,
)

from netvault.cli.pdf_fingerprint import pdf_content_fingerprint


def make_pdf(path: Path, *, doi="10.1234/example", text=b"Article body",
             notice=False, metadata="first", compressed=False, image=b"abc",
             extra_notice=b"", preallocate=False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    writer = PdfWriter()
    if preallocate:
        writer._add_object(DictionaryObject({NameObject('/Unused'): NumberObject(123)}))
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)}),
    })
    contents = StreamObject()
    data = b'BT /F1 12 Tf 1 0 0 1 50 700 Tm (' + text + b') Tj ET\n'
    xobjects = DictionaryObject()
    picture = StreamObject()
    picture.set_data(image)
    picture.update({NameObject('/Type'): NameObject('/XObject'),
                    NameObject('/Subtype'): NameObject('/Image'),
                    NameObject('/Width'): NumberObject(1), NameObject('/Height'): NumberObject(1),
                    NameObject('/ColorSpace'): NameObject('/DeviceRGB'),
                    NameObject('/BitsPerComponent'): NumberObject(8)})
    xobjects[NameObject('/Image')] = writer._add_object(picture)
    data += b'q 10 0 0 10 40 40 cm /Image Do Q\n'
    if notice:
        form = StreamObject()
        statement = (
            ' 14734192, 2013, 1, Downloaded from https://onlinelibrary.wiley.com/doi/'
            f'{doi} by University A, Wiley Online Library on [09/10/2026]. '
            'See the Terms and Conditions \\(https://onlinelibrary.wiley.com/terms-and-conditions\\) '
            'on Wiley Online Library for rules of use; OA articles are governed by '
            'the applicable Creative Commons License'
        ).encode()
        form.set_data(b'BT /F1 4 Tf 0 -1 1 0 590 790 Tm (' + statement + b') Tj ET\n'
                      + extra_notice)
        form.update({NameObject('/Type'): NameObject('/XObject'),
                     NameObject('/Subtype'): NameObject('/Form'),
                     NameObject('/BBox'): ArrayObject([NumberObject(x) for x in (0, 0, 600, 800)]),
                     NameObject('/Resources'): page['/Resources']})
        # Give the Form its own font resources rather than a cyclic XObject dictionary.
        form[NameObject('/Resources')] = DictionaryObject({
            NameObject('/Font'): page['/Resources']['/Font'],
        })
        xobjects[NameObject('/Notice')] = writer._add_object(form)
        data += b'q /Notice Do Q\n'
    page['/Resources'][NameObject('/XObject')] = xobjects
    contents.set_data(data)
    page[NameObject('/Contents')] = writer._add_object(contents)
    if compressed:
        page.compress_content_streams()
    writer.add_metadata({'/Title': metadata, '/ArticleDOI': doi})
    writer.write(path)
    return path


def test_metadata_object_numbers_and_compression_do_not_change_fingerprint(tmp_path):
    first = make_pdf(tmp_path / 'a.pdf')
    second = make_pdf(tmp_path / 'b.pdf', metadata='different', preallocate=True, compressed=True)
    assert first.read_bytes() != second.read_bytes()
    assert pdf_content_fingerprint(first) is not None
    assert pdf_content_fingerprint(first) == pdf_content_fingerprint(second)


def test_only_verified_wiley_download_notice_is_ignored(tmp_path):
    first = make_pdf(tmp_path / 'a.pdf', notice=True)
    second = make_pdf(tmp_path / 'b.pdf', doi='10.1234/example_2', notice=True, metadata='other')
    assert pdf_content_fingerprint(first) is not None
    assert pdf_content_fingerprint(first) == pdf_content_fingerprint(second)


@pytest.mark.parametrize('changes', [
    {'text': b'Changed article body'}, {'image': b'xyz'},
    {'extra_notice': b'BT /F1 12 Tf (Additional scientific result) Tj ET'},
    {'extra_notice': b'q /Image Do Q'},
])
def test_text_images_and_additional_form_content_remain_distinct(tmp_path, changes):
    first = make_pdf(tmp_path / 'a.pdf', notice=True)
    second = make_pdf(tmp_path / 'b.pdf', notice=True, **changes)
    assert pdf_content_fingerprint(first) is not None
    assert pdf_content_fingerprint(first) != pdf_content_fingerprint(second)


def test_unreadable_blank_and_encrypted_pdfs_are_not_content_deduplicated(tmp_path):
    invalid = tmp_path / 'invalid.pdf'
    invalid.write_bytes(b'%PDF-not-readable')
    assert pdf_content_fingerprint(invalid) is None
    writer = PdfWriter()
    writer.add_blank_page(600, 800)
    blank = tmp_path / 'blank.pdf'
    writer.write(blank)
    assert pdf_content_fingerprint(blank) is None
    writer.encrypt('password')
    locked = tmp_path / 'encrypted.pdf'
    writer.write(locked)
    assert pdf_content_fingerprint(locked) is None
