from sqlalchemy import func, literal
from sqlalchemy.orm import joinedload, load_only

from netvault_server.server.models import Pdf, User


def pdf_search_text():
    fields = (
        Pdf.doi,
        Pdf.title,
        Pdf.authors,
        Pdf.container_title,
        Pdf.publisher,
        Pdf.original_name,
        Pdf.sha256,
    )
    expression = func.coalesce(fields[0], "")
    for field in fields[1:]:
        expression = expression + literal(" ") + func.coalesce(field, "")
    return func.lower(expression)


def pdf_contains_query(query: str):
    return pdf_search_text().contains(query.casefold(), autoescape=True)


def pdf_read_options():
    return (
        load_only(
            Pdf.id,
            Pdf.doi,
            Pdf.doi_source,
            Pdf.sha256,
            Pdf.original_name,
            Pdf.title,
            Pdf.authors,
            Pdf.container_title,
            Pdf.publisher,
            Pdf.published_year,
            Pdf.crossref_status,
            Pdf.crossref_url,
            Pdf.size,
            Pdf.uploaded_at,
            Pdf.uploaded_by_id,
        ),
        joinedload(Pdf.uploaded_by).load_only(User.username),
    )
