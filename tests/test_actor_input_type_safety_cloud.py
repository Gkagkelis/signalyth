from app.services.integrations import sanitize_actor_input_types


def test_string_field_rejects_object_example_instead_of_sending_invalid_actor_input():
    schema = {"type": "object", "properties": {"@": {"type": "string"}}, "required": ["@"]}
    cleaned, mismatches = sanitize_actor_input_types(schema, {"@": {"url": "https://example.com"}})
    assert "@" not in cleaned
    assert mismatches == ["@"]


def test_schema_sanitizer_keeps_and_safely_coerces_simple_values():
    schema = {"properties": {"q": {"type": "string"}, "max": {"type": "integer"}, "tags": {"type": "array"}}}
    cleaned, mismatches = sanitize_actor_input_types(schema, {"q": 123, "max": "3", "tags": "one"})
    assert cleaned == {"q": "123", "max": 3, "tags": ["one"]}
    assert mismatches == []
