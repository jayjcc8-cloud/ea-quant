//! Canonical JSON bytes with the project's Python semantics.
//!
//! `canonical_json_bytes` renders one JSON value as compact ASCII JSON with
//! sorted object keys, matching Python's
//! `json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
//! separators=(",", ":"))`. Economic values are already canonical decimal
//! strings; a JSON float is refused rather than re-encoded through binary
//! floating point.

use serde_json::Value;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum CanonicalError {
    UnsupportedNumber,
}

impl std::fmt::Display for CanonicalError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CanonicalError::UnsupportedNumber => {
                write!(
                    f,
                    "a JSON number with a fraction or exponent is not canonical input"
                )
            }
        }
    }
}

impl std::error::Error for CanonicalError {}

pub fn canonical_json_bytes(value: &Value) -> Result<Vec<u8>, CanonicalError> {
    let mut out = Vec::new();
    write_value(value, &mut out)?;
    Ok(out)
}

fn write_value(value: &Value, out: &mut Vec<u8>) -> Result<(), CanonicalError> {
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(number) => write_number(number, out)?,
        Value::String(text) => write_string(text, out),
        Value::Array(items) => {
            out.push(b'[');
            for (index, item) in items.iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_value(item, out)?;
            }
            out.push(b']');
        }
        Value::Object(map) => {
            out.push(b'{');
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_unstable();
            for (index, key) in keys.into_iter().enumerate() {
                if index > 0 {
                    out.push(b',');
                }
                write_string(key, out);
                out.push(b':');
                write_value(&map[key], out)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

fn write_number(number: &serde_json::Number, out: &mut Vec<u8>) -> Result<(), CanonicalError> {
    if let Some(integer) = number.as_i64() {
        out.extend_from_slice(integer.to_string().as_bytes());
        return Ok(());
    }
    if let Some(integer) = number.as_u64() {
        out.extend_from_slice(integer.to_string().as_bytes());
        return Ok(());
    }
    Err(CanonicalError::UnsupportedNumber)
}

fn write_string(text: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for character in text.chars() {
        match character {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\u{0c}' => out.extend_from_slice(b"\\f"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\r' => out.extend_from_slice(b"\\r"),
            '\t' => out.extend_from_slice(b"\\t"),
            character if (character as u32) < 0x20 || (character as u32) >= 0x7f => {
                let code = character as u32;
                if code < 0x10000 {
                    out.extend_from_slice(format!("\\u{code:04x}").as_bytes());
                } else {
                    let code = code - 0x10000;
                    let high = 0xd800 | ((code >> 10) & 0x3ff);
                    let low = 0xdc00 | (code & 0x3ff);
                    out.extend_from_slice(format!("\\u{high:04x}\\u{low:04x}").as_bytes());
                }
            }
            character => out.push(character as u8),
        }
    }
    out.push(b'"');
}
