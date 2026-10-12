//! Bounded local IPC: one private Unix socket inside the core's own workspace.
//!
//! The socket is the only client surface, and it serves exactly the confirmed
//! experiment methods. It never exposes result submission, an arbitrary worker
//! command, a trusted path, a test hook, or a caller-supplied stop proof.

use std::io::{self, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};

/// The longest single request line this core will buffer.
pub const MAX_LINE: usize = 1 << 20;

pub fn socket_path(workspace: &Path) -> PathBuf {
    workspace.join("core.sock")
}

/// Bind the private socket. The caller already holds the workspace lock, so any
/// file still at this path belongs to a workspace that is not being served.
pub fn bind(path: &Path) -> io::Result<UnixListener> {
    match std::fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let listener = UnixListener::bind(path)?;
    listener.set_nonblocking(true)?;
    Ok(listener)
}

pub fn connect(path: &Path) -> io::Result<UnixStream> {
    UnixStream::connect(path)
}

/// One client connection: bounded input, bounded output, never blocking.
pub struct Conn {
    stream: UnixStream,
    input: Vec<u8>,
    output: Vec<u8>,
    pub closed: bool,
    /// Set once a client sends a line longer than [`MAX_LINE`].
    oversized: bool,
}

impl Conn {
    pub fn new(stream: UnixStream) -> io::Result<Conn> {
        stream.set_nonblocking(true)?;
        Ok(Conn {
            stream,
            input: Vec::new(),
            output: Vec::new(),
            closed: false,
            oversized: false,
        })
    }

    /// Read whatever is available and return every complete line it completed.
    pub fn poll_lines(&mut self) -> Vec<String> {
        let mut buffer = [0u8; 8192];
        loop {
            match self.stream.read(&mut buffer) {
                Ok(0) => {
                    self.closed = true;
                    break;
                }
                Ok(read) => {
                    self.input.extend_from_slice(&buffer[..read]);
                    if self.input.len() > MAX_LINE {
                        self.oversized = true;
                        self.closed = true;
                        break;
                    }
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    self.closed = true;
                    break;
                }
            }
        }
        let mut lines = Vec::new();
        while let Some(index) = self.input.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = self.input.drain(..=index).collect();
            let text = String::from_utf8_lossy(&line[..line.len() - 1]).to_string();
            if !text.trim().is_empty() {
                lines.push(text);
            }
        }
        lines
    }

    pub fn queue(&mut self, mut payload: Vec<u8>) {
        payload.push(b'\n');
        self.output.extend_from_slice(&payload);
    }

    /// Write the queued replies without ever blocking on a slow client.
    pub fn flush(&mut self) {
        while !self.output.is_empty() {
            match self.stream.write(&self.output) {
                Ok(0) => {
                    self.closed = true;
                    break;
                }
                Ok(written) => {
                    self.output.drain(..written);
                }
                Err(error) if error.kind() == io::ErrorKind::WouldBlock => break,
                Err(_) => {
                    self.closed = true;
                    break;
                }
            }
        }
    }

    pub fn oversized(&self) -> bool {
        self.oversized
    }
}
