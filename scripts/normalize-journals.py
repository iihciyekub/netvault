#!/usr/bin/env python3
"""Apply an explicitly reviewed journal identity plan, retaining raw PDF metadata.

Usage: NETVAULT_DATABASE_URL=... python scripts/normalize-journals.py --plan plan.json --apply
Without --apply, this prints the plan counts without connecting to a database.
"""

import argparse
import json
from pathlib import Path

from sqlalchemy import text

from netvault_server.server.journal_identity import journal_display_name, serial_identifiers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    plan = json.loads(args.plan.read_text())
    for name in plan['canonical_names']:
        if name != journal_display_name(name):
            raise ValueError(f'Canonical name is not normalized: {name!r}')
    print(json.dumps({key: len(plan[key]) for key in (
        'canonical_names', 'aliases', 'identifiers', 'samples', 'doi_overrides',
    )}))
    if not args.apply:
        return
    from netvault_server.server.database import engine

    if engine.dialect.name != 'postgresql':
        raise RuntimeError('The audited bulk update requires PostgreSQL')
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = 0"))
        connection.execute(text("SET LOCAL lock_timeout = '5s'"))
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('netvault-journal-normalization'))"))
        before = dict(connection.execute(text(
            'SELECT count(*) AS pdfs, count(*) FILTER (WHERE NOT is_deleted) AS active, '
            'count(DISTINCT container_title) AS names FROM pdfs'
        )).mappings().one())
        connection.execute(text(
            'INSERT INTO journal_canonical_names (canonical_name) VALUES (:name) ON CONFLICT DO NOTHING'
        ), [{'name': name} for name in plan['canonical_names']])
        for key, table, column in (
            ('aliases', 'journal_name_aliases', 'alias_name'),
            ('identifiers', 'journal_issn_identifiers', 'issn'),
            ('doi_overrides', 'journal_doi_overrides', 'doi'),
        ):
            for record in plan[key]:
                if key == 'identifiers' and serial_identifiers([record['issn']]) != (record['issn'],):
                    raise ValueError(f'Invalid ISSN: {record["issn"]}')
                existing = connection.scalar(text(
                    f'SELECT canonical_name FROM {table} WHERE {column} = :value'
                ), {'value': record[column]})
                if existing and existing != record['canonical_name']:
                    raise ValueError(f'Existing identity conflict: {record[column]} -> {existing}')
            connection.execute(text(
                f'INSERT INTO {table} ({column}, canonical_name, evidence_json) '
                'VALUES (:value, :name, :evidence) '
                f'ON CONFLICT ({column}) DO UPDATE SET evidence_json = EXCLUDED.evidence_json'
            ), [{'value': row[column], 'name': row['canonical_name'],
                  'evidence': json.dumps(row['evidence'], ensure_ascii=False)} for row in plan[key]])

        samples = {row['doi']: row for row in plan['samples']}
        for alias in plan['aliases']:
            ev = alias['evidence']
            if ev.get('method') == 'publisher-and-issn-registry-reviewed' and (
                alias['alias_name'] in {'MANAGEMENT SCIENCES', 'MARKETING SCIENCE (PROVIDENCE, R.I.)'}
            ):
                for doi in ev['dois']:
                    samples[doi] = {'doi': doi, 'issns': ev['issns'], 'work_type': 'journal-article'}
        for override in plan['doi_overrides']:
            samples[override['doi']] = {
                'doi': override['doi'], 'issns': override['issns'], 'work_type': 'journal-article',
            }
        connection.execute(text(
            'UPDATE pdfs SET journal_issns_json = :issns, work_type = :type WHERE doi = :doi'
        ), [{'doi': row['doi'], 'issns': json.dumps(row['issns']), 'type': row['work_type']}
            for row in samples.values()])
        result = connection.execute(text(
            'UPDATE pdfs p SET container_title = a.canonical_name '
            'FROM journal_name_aliases a '
            'WHERE netvault_journal_display_name(p.container_title) = a.alias_name '
            'AND p.container_title IS DISTINCT FROM a.canonical_name'
        ))
        after = dict(connection.execute(text(
            'SELECT count(*) AS pdfs, count(*) FILTER (WHERE NOT is_deleted) AS active, '
            'count(DISTINCT container_title) AS names FROM pdfs'
        )).mappings().one())
        if (before['pdfs'], before['active']) != (after['pdfs'], after['active']):
            raise RuntimeError('PDF counts changed; rolling back')
        invalid = connection.scalar(text(
            'SELECT count(*) FROM pdfs WHERE '
            'container_title IS DISTINCT FROM netvault_journal_display_name(container_title)'
        ))
        if invalid:
            raise RuntimeError('Non-normalized names remain; rolling back')
        print(json.dumps({'before': before, 'after': after,
                          'alias_rows_updated': result.rowcount, 'samples_updated': len(samples)}))


if __name__ == '__main__':
    main()
