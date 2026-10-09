//! Small byte/identity helpers with no external hex dependency.

use sha2::{Digest, Sha256};

pub fn hex_encode(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        out.push_str(&format!("{byte:02x}"));
    }
    out
}

pub fn hex_decode(text: &str) -> Option<Vec<u8>> {
    if text.len() % 2 != 0 {
        return None;
    }
    let mut out = Vec::with_capacity(text.len() / 2);
    let mut high = true;
    let mut current: u8 = 0;
    for character in text.bytes() {
        let value = match character {
            b'0'..=b'9' => character - b'0',
            b'a'..=b'f' => character - b'a' + 10,
            _ => return None,
        };
        if high {
            current = value << 4;
        } else {
            current |= value;
            out.push(current);
        }
        high = !high;
    }
    Some(out)
}

pub fn sha256_hex(bytes: &[u8]) -> String {
    hex_encode(&Sha256::digest(bytes))
}
