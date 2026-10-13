-- Shared identity contract: HTML5 decoding, ISSN validation and direct-SQL enforcement.
CREATE OR REPLACE FUNCTION netvault_html_unescape(raw_name text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE result text := ''; token text; decoded text; remaining text := raw_name;
        matched text[]; amp integer; used integer; n bigint; digit integer; letter text;
BEGIN
    IF raw_name IS NULL OR strpos(raw_name, '&') = 0 THEN RETURN raw_name; END IF;
    WHILE remaining <> '' LOOP
        amp := strpos(remaining, '&');
        IF amp = 0 THEN result := result || remaining; EXIT; END IF;
        result := result || left(remaining, amp - 1);
        remaining := substr(remaining, amp);
        matched := regexp_match(remaining, '^&(#[xX][0-9a-fA-F]+;?|#[0-9]+;?|[^[:space:]<&#;]{1,32};?)');
        IF matched IS NULL THEN
            result := result || '&'; remaining := substr(remaining, 2); CONTINUE;
        END IF;
        token := matched[1]; decoded := NULL; used := length(token);
        IF left(token, 1) = '#' THEN
            token := rtrim(substr(token, 2), ';'); n := 0;
            IF left(token, 1) IN ('x','X') THEN
                FOR letter IN SELECT regexp_split_to_table(lower(substr(token, 2)), '') LOOP
                    digit := strpos('0123456789abcdef', letter) - 1;
                    n := n * 16 + digit; EXIT WHEN n > 1114111;
                END LOOP;
            ELSE
                FOR letter IN SELECT regexp_split_to_table(token, '') LOOP
                    n := n * 10 + letter::integer; EXIT WHEN n > 1114111;
                END LOOP;
            END IF;
            decoded := CASE n
            WHEN 0 THEN chr(65533)
            WHEN 13 THEN chr(13)
            WHEN 128 THEN chr(8364)
            WHEN 129 THEN chr(129)
            WHEN 130 THEN chr(8218)
            WHEN 131 THEN chr(402)
            WHEN 132 THEN chr(8222)
            WHEN 133 THEN chr(8230)
            WHEN 134 THEN chr(8224)
            WHEN 135 THEN chr(8225)
            WHEN 136 THEN chr(710)
            WHEN 137 THEN chr(8240)
            WHEN 138 THEN chr(352)
            WHEN 139 THEN chr(8249)
            WHEN 140 THEN chr(338)
            WHEN 141 THEN chr(141)
            WHEN 142 THEN chr(381)
            WHEN 143 THEN chr(143)
            WHEN 144 THEN chr(144)
            WHEN 145 THEN chr(8216)
            WHEN 146 THEN chr(8217)
            WHEN 147 THEN chr(8220)
            WHEN 148 THEN chr(8221)
            WHEN 149 THEN chr(8226)
            WHEN 150 THEN chr(8211)
            WHEN 151 THEN chr(8212)
            WHEN 152 THEN chr(732)
            WHEN 153 THEN chr(8482)
            WHEN 154 THEN chr(353)
            WHEN 155 THEN chr(8250)
            WHEN 156 THEN chr(339)
            WHEN 157 THEN chr(157)
            WHEN 158 THEN chr(382)
            WHEN 159 THEN chr(376)
                ELSE CASE
                    WHEN n > 1114111 OR n BETWEEN 55296 AND 57343 THEN chr(65533)
                    WHEN n BETWEEN 1 AND 8 OR n IN (11,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,31,127)
                        OR n BETWEEN 64976 AND 65007 OR n % 65536 IN (65534,65535) THEN ''
                    ELSE chr(n::integer)
                END
            END;
        ELSE
            FOR candidate_length IN REVERSE length(token)..1 LOOP
                SELECT replacement INTO decoded FROM netvault_html_entities WHERE name = left(token, candidate_length);
                IF FOUND THEN used := candidate_length; EXIT; END IF;
            END LOOP;
            IF decoded IS NULL THEN used := length(token); END IF;
        END IF;
        IF decoded IS NULL THEN result := result || '&' || matched[1];
        ELSE result := result || decoded; END IF;
        remaining := substr(remaining, used + 2);
    END LOOP;
    RETURN result;
END;
$$;

CREATE OR REPLACE FUNCTION netvault_journal_display_name(raw_name text)
RETURNS text LANGUAGE plpgsql STABLE AS $$
DECLARE result text := raw_name; previous text; expansion record;
BEGIN
    IF result IS NULL THEN RETURN NULL; END IF;
    FOR i IN 1..4 LOOP
        previous := result; result := netvault_html_unescape(result);
        EXIT WHEN result = previous;
    END LOOP;
    result := normalize(result, NFKC);
    -- libc upper() does not expand characters such as sharp-s like Python.
    -- Ordinary ASCII names avoid this small Unicode lookup entirely.
    IF result ~ '[^ -~]' THEN
        FOR expansion IN SELECT character,replacement FROM netvault_upper_expansions
            WHERE strpos(result, character) > 0 LOOP
            result := replace(result, expansion.character, expansion.replacement);
        END LOOP;
    END IF;
    RETURN nullif(upper(btrim(regexp_replace(result, '[[:space:]]+', ' ', 'g'))), '');
END;
$$;

CREATE OR REPLACE FUNCTION netvault_normalize_issn(raw_issn text)
RETURNS text LANGUAGE plpgsql IMMUTABLE AS $$
DECLARE digits text := upper(regexp_replace(raw_issn, '[[:space:]-]', '', 'g')); total integer;
BEGIN
    IF digits IS NULL OR digits !~ '^[0-9]{7}[0-9X]$' THEN RETURN NULL; END IF;
    total := CASE right(digits, 1) WHEN 'X' THEN 10 ELSE right(digits,1)::integer END;
    FOR i IN 1..7 LOOP total := total + substr(digits,i,1)::integer * (9-i); END LOOP;
    IF total % 11 <> 0 THEN RETURN NULL; END IF;
    RETURN left(digits,4) || '-' || right(digits,4);
END;
$$;

CREATE OR REPLACE FUNCTION netvault_enforce_journal_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE display_name text; resolved_name text; match_count integer;
        identifiers jsonb := '[]'::jsonb;
BEGIN
    display_name := netvault_journal_display_name(NEW.container_title);
    IF NEW.journal_raw_title IS NULL THEN NEW.journal_raw_title := NEW.container_title; END IF;
    BEGIN
        identifiers := coalesce(NEW.journal_issns_json::jsonb, '[]'::jsonb);
        IF jsonb_typeof(identifiers) <> 'array' THEN identifiers := '[]'::jsonb; END IF;
    EXCEPTION WHEN invalid_text_representation THEN identifiers := '[]'::jsonb;
    END;
    SELECT coalesce(jsonb_agg(issn ORDER BY issn), '[]'::jsonb) INTO identifiers
      FROM (SELECT DISTINCT netvault_normalize_issn(value) AS issn
            FROM jsonb_array_elements_text(identifiers)) valid WHERE issn IS NOT NULL;
    IF NEW.journal_issns_json IS NOT NULL THEN NEW.journal_issns_json := identifiers::text; END IF;
    IF jsonb_array_length(identifiers) > 0 THEN
        SELECT count(DISTINCT canonical_name), min(canonical_name) INTO match_count, resolved_name
          FROM journal_issn_identifiers WHERE issn IN (SELECT jsonb_array_elements_text(identifiers));
        IF match_count <> 1 THEN resolved_name := NULL; END IF;
    ELSE
        SELECT canonical_name INTO resolved_name FROM journal_doi_overrides WHERE doi = NEW.doi;
        IF resolved_name IS NULL THEN
            SELECT canonical_name INTO resolved_name FROM journal_name_aliases WHERE alias_name = display_name;
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
    container_title, journal_issns_json, journal_raw_title, journal_key, doi ON pdfs
    FOR EACH ROW EXECUTE FUNCTION netvault_enforce_journal_identity();
