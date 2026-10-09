//! Cross-language canonical encoding identity.
//!
//! These bytes match the project's Python `canonical_json_bytes` semantics:
//! compact ASCII JSON, sorted object keys, and no invented numeric conversion.
//! Economic values are already canonical decimal strings and are never turned
//! into floating point here.

use quine_core::canonical::canonical_json_bytes;

#[test]
fn canonical_bytes_match_python_compact_sorted_ascii() {
    // The exact byte string Python produces for this document with
    // `json.dumps(doc, ensure_ascii=True, allow_nan=False, sort_keys=True,
    // separators=(",", ":"))`.
    let document: serde_json::Value = serde_json::json!({
        "z": "1",
        "a": {"y": 2, "x": "3.10"},
        "m": [1, "two", null, false],
    });
    let expected = br#"{"a":{"x":"3.10","y":2},"m":[1,"two",null,false],"z":"1"}"#;
    assert_eq!(canonical_json_bytes(&document).unwrap(), expected.to_vec());
}

#[test]
fn canonical_bytes_keep_economic_strings_untouched() {
    // A decimal string that looks like a float must never be re-encoded as a
    // number; it stays the exact string bytes.
    let document: serde_json::Value = serde_json::json!({"initial_cash": "10000.00"});
    assert_eq!(
        canonical_json_bytes(&document).unwrap(),
        br#"{"initial_cash":"10000.00"}"#.to_vec()
    );
}
