-- Database enforcement for direct SQL, older upload clients, and ORM writes.
CREATE OR REPLACE FUNCTION netvault_journal_display_name(raw_name text)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE result text := raw_name; previous text;
BEGIN
    IF result IS NULL THEN RETURN NULL; END IF;
    FOR i IN 1..4 LOOP
        previous := result;
        result := replace(replace(replace(replace(replace(replace(
            replace(replace(result, '&amp;', '&'), '&quot;', '"'), '&apos;', ''''),
            '&#39;', ''''), '&lt;', '<'), '&gt;', '>'), '&nbsp;', ' '), chr(160), ' ');
        EXIT WHEN result = previous;
    END LOOP;
    RETURN nullif(upper(btrim(regexp_replace(normalize(result, NFKC), '[[:space:]]+', ' ', 'g'))), '');
END;
$$;

CREATE OR REPLACE FUNCTION netvault_enforce_journal_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE display_name text; resolved_name text; match_count integer;
        identifiers jsonb := '[]'::jsonb;
BEGIN
    display_name := netvault_journal_display_name(NEW.container_title);
    IF NEW.journal_raw_title IS NULL THEN
        NEW.journal_raw_title := NEW.container_title;
    END IF;
    BEGIN
        identifiers := coalesce(NEW.journal_issns_json::jsonb, '[]'::jsonb);
        IF jsonb_typeof(identifiers) <> 'array' THEN identifiers := '[]'::jsonb; END IF;
    EXCEPTION WHEN invalid_text_representation THEN
        identifiers := '[]'::jsonb;
    END;
    IF jsonb_array_length(identifiers) > 0 THEN
        SELECT count(DISTINCT canonical_name), min(canonical_name)
          INTO match_count, resolved_name
          FROM journal_issn_identifiers
          WHERE issn IN (SELECT jsonb_array_elements_text(identifiers));
        IF match_count <> 1 THEN resolved_name := NULL; END IF;
    ELSE
        SELECT canonical_name INTO resolved_name FROM journal_doi_overrides WHERE doi = NEW.doi;
        IF resolved_name IS NULL THEN
            SELECT canonical_name INTO resolved_name FROM journal_name_aliases
              WHERE alias_name = display_name;
        END IF;
    END IF;
    NEW.container_title := coalesce(resolved_name, display_name);
    NEW.journal_key := nullif(btrim(regexp_replace(
        regexp_replace(replace(lower(NEW.container_title), '&', ' and '), '\mthe\M', ' ', 'g'),
        '[^a-z0-9]+', ' ', 'g')), '');
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_netvault_journal_identity ON pdfs;
CREATE TRIGGER trg_netvault_journal_identity BEFORE INSERT OR UPDATE OF
    container_title, journal_issns_json, journal_raw_title, doi ON pdfs
    FOR EACH ROW EXECUTE FUNCTION netvault_enforce_journal_identity();
