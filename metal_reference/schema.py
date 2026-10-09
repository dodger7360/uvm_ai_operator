"""SQLite schema for the Metal Reference domain."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS reference_source (
    id INTEGER PRIMARY KEY,
    source_type TEXT NOT NULL,
    name TEXT NOT NULL,
    url_path TEXT,
    metadata TEXT
);

CREATE TABLE IF NOT EXISTS reference_family (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reference_category (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES reference_family(id) ON DELETE RESTRICT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    external_id TEXT,
    CHECK (external_id IS NULL OR source_id IS NOT NULL),
    UNIQUE (source_id, external_id)
);

CREATE TABLE IF NOT EXISTS reference_standard (
    id INTEGER PRIMARY KEY,
    designation TEXT NOT NULL,
    document_type TEXT NOT NULL,
    year INTEGER,
    name TEXT,
    url TEXT,
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    source_locator TEXT,
    UNIQUE (designation, document_type, year)
);

CREATE TABLE IF NOT EXISTS reference_item (
    id INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES reference_category(id) ON DELETE RESTRICT,
    normalized_key TEXT NOT NULL,
    designation TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    external_id TEXT,
    CHECK (external_id IS NULL OR source_id IS NOT NULL),
    UNIQUE (category_id, normalized_key),
    UNIQUE (category_id, source_id, external_id)
);

CREATE TABLE IF NOT EXISTS reference_variant (
    id INTEGER PRIMARY KEY,
    reference_item_id INTEGER NOT NULL REFERENCES reference_item(id) ON DELETE CASCADE,
    grade_class TEXT,
    execution TEXT,
    form TEXT,
    standard_id INTEGER REFERENCES reference_standard(id) ON DELETE RESTRICT,
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    source_locator TEXT
);

CREATE TABLE IF NOT EXISTS reference_dimension (
    id INTEGER PRIMARY KEY,
    reference_item_id INTEGER REFERENCES reference_item(id) ON DELETE CASCADE,
    reference_variant_id INTEGER REFERENCES reference_variant(id) ON DELETE CASCADE,
    parameter_type TEXT,
    numeric_value REAL,
    unit TEXT,
    raw_label TEXT,
    raw_value TEXT,
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    source_locator TEXT,
    CHECK ((reference_item_id IS NOT NULL) <> (reference_variant_id IS NOT NULL)),
    CHECK (numeric_value IS NULL OR unit IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS reference_property (
    id INTEGER PRIMARY KEY,
    reference_item_id INTEGER REFERENCES reference_item(id) ON DELETE CASCADE,
    reference_variant_id INTEGER REFERENCES reference_variant(id) ON DELETE CASCADE,
    property_type TEXT NOT NULL,
    numeric_value REAL,
    unit TEXT NOT NULL,
    raw_label TEXT,
    raw_value TEXT,
    context TEXT,
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    source_locator TEXT,
    CHECK ((reference_item_id IS NOT NULL) <> (reference_variant_id IS NOT NULL))
);

CREATE TABLE IF NOT EXISTS reference_alias (
    id INTEGER PRIMARY KEY,
    reference_category_id INTEGER REFERENCES reference_category(id) ON DELETE CASCADE,
    reference_item_id INTEGER REFERENCES reference_item(id) ON DELETE CASCADE,
    reference_variant_id INTEGER REFERENCES reference_variant(id) ON DELETE CASCADE,
    raw_value TEXT NOT NULL,
    normalized_value TEXT NOT NULL,
    alias_type TEXT NOT NULL,
    source_id INTEGER REFERENCES reference_source(id) ON DELETE RESTRICT,
    source_locator TEXT,
    CHECK (
        (reference_category_id IS NOT NULL) +
        (reference_item_id IS NOT NULL) +
        (reference_variant_id IS NOT NULL) = 1
    )
);

CREATE INDEX IF NOT EXISTS idx_reference_item_category
    ON reference_item(category_id);
CREATE INDEX IF NOT EXISTS idx_reference_category_source
    ON reference_category(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_category_family
    ON reference_category(family_id);
CREATE INDEX IF NOT EXISTS idx_reference_item_source
    ON reference_item(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_variant_item
    ON reference_variant(reference_item_id);
CREATE INDEX IF NOT EXISTS idx_reference_variant_standard
    ON reference_variant(standard_id);
CREATE INDEX IF NOT EXISTS idx_reference_variant_source
    ON reference_variant(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_dimension_item
    ON reference_dimension(reference_item_id);
CREATE INDEX IF NOT EXISTS idx_reference_dimension_variant
    ON reference_dimension(reference_variant_id);
CREATE INDEX IF NOT EXISTS idx_reference_dimension_type
    ON reference_dimension(parameter_type);
CREATE INDEX IF NOT EXISTS idx_reference_dimension_source
    ON reference_dimension(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_property_item
    ON reference_property(reference_item_id);
CREATE INDEX IF NOT EXISTS idx_reference_property_variant
    ON reference_property(reference_variant_id);
CREATE INDEX IF NOT EXISTS idx_reference_property_type
    ON reference_property(property_type);
CREATE INDEX IF NOT EXISTS idx_reference_property_source
    ON reference_property(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_standard_source
    ON reference_standard(source_id);
CREATE INDEX IF NOT EXISTS idx_reference_alias_category
    ON reference_alias(reference_category_id);
CREATE INDEX IF NOT EXISTS idx_reference_alias_item
    ON reference_alias(reference_item_id);
CREATE INDEX IF NOT EXISTS idx_reference_alias_variant
    ON reference_alias(reference_variant_id);
CREATE INDEX IF NOT EXISTS idx_reference_alias_normalized
    ON reference_alias(normalized_value);
CREATE INDEX IF NOT EXISTS idx_reference_alias_source
    ON reference_alias(source_id);
"""
