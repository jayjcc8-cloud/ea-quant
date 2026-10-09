//! Direct supervision of one owned child process group.
//!
//! The core spawns the existing Python worker (and its stateless domain
//! validator) itself: it captures the child's own process group from the live
//! spawn, keeps the write end of a liveness pipe that the child inherits the
//! read end of, and signals only the group it captured — never a process id
//! read from disk. A group whose identity was never proven is never signalled
//! and never reported stopped: an unproven group stays unknown.

use std::io;
use std::os::fd::{FromRawFd, OwnedFd, RawFd};
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// Grace given to SIGTERM before the whole group is killed.
pub const STOP_GRACE: Duration = Duration::from_millis(300);
/// Grace given to SIGKILL before the group is reported unconfirmed.
pub const KILL_GRACE: Duration = Duration::from_millis(300);

/// What one supervision tick proved about an owned child.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GuardState {
    /// The child or its group is still there.
    Running,
    /// The leader was reaped and the whole group is proven gone.
    Stopped,
    /// A stop was accepted but the group outlived SIGKILL.
    Unconfirmed,
    /// No stop was accepted, yet the group could not be shown to be gone.
    Abandoned,
}

pub struct ChildGuard {
    child: Child,
    /// The verified process group of this child, or `None` when that identity
    /// could not be captured. Only a group captured here is ever signalled.
    group: Option<i32>,
    /// Write end of the liveness pipe. Held for exactly as long as this core
    /// owns the child: closing it (or dying) tells the child its owner is gone.
    _liveness: Option<OwnedFd>,
    leader_exited: bool,
    /// Set when a reap this core attempted did not complete. A reap that could
    /// not be performed is not proof that the child exited, so no stop is
    /// claimed while it holds.
    reap_uncertain: bool,
    stop_accepted: bool,
    term_sent: bool,
    kill_sent: bool,
    term_deadline: Option<Instant>,
    kill_deadline: Option<Instant>,
}

impl ChildGuard {
    /// Spawn one child in its own process group with an optionally inherited
    /// liveness channel, appending `--parent-fd` when one is created.
    pub fn spawn(argv: &[String], cwd: Option<&Path>, liveness: bool) -> io::Result<ChildGuard> {
        if argv.is_empty() {
            return Err(io::Error::new(io::ErrorKind::InvalidInput, "empty argv"));
        }
        let (read_fd, write_fd) = if liveness {
            create_pipe()?
        } else {
            (None, None)
        };
        let mut command = Command::new(&argv[0]);
        command
            .args(&argv[1..])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            // The child leads its own process group, captured below from the
            // live spawn handle.
            .process_group(0);
        if let Some(cwd) = cwd {
            command.current_dir(cwd);
        }
        if let Some(fd) = read_fd {
            command.arg("--parent-fd").arg(fd.to_string());
        }
        match command.spawn() {
            Ok(child) => {
                if let Some(fd) = read_fd {
                    // The child holds it now; the parent must not keep a copy.
                    drop(unsafe { OwnedFd::from_raw_fd(fd) });
                }
                let group = group_of(child.id());
                Ok(ChildGuard {
                    child,
                    group,
                    _liveness: write_fd,
                    leader_exited: false,
                    reap_uncertain: false,
                    stop_accepted: false,
                    term_sent: false,
                    kill_sent: false,
                    term_deadline: None,
                    kill_deadline: None,
                })
            }
            Err(error) => {
                if let Some(fd) = read_fd {
                    drop(unsafe { OwnedFd::from_raw_fd(fd) });
                }
                Err(error)
            }
        }
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    /// Advance one bounded supervision step and report what is proven.
    pub fn poll(&mut self, stop_requested: bool) -> GuardState {
        if !self.leader_exited {
            match self.child.try_wait() {
                Ok(Some(_)) => {
                    self.leader_exited = true;
                    self.reap_uncertain = false;
                }
                Ok(None) => {
                    self.reap_uncertain = false;
                }
                // A reap that did not complete proves nothing about the child.
                // Treating it as an exit would let an unproven process be
                // reported stopped, so it is carried as an open uncertainty.
                Err(_) => self.reap_uncertain = true,
            }
        }
        if stop_requested {
            self.stop_accepted = true;
        }
        if self.group.is_some() && self.leader_exited && !self.reap_uncertain && !self.group_alive()
        {
            return GuardState::Stopped;
        }
        let now = Instant::now();
        if !self.term_sent && (self.stop_accepted || self.leader_exited) {
            self.term_sent = true;
            if self.stop_accepted {
                self.signal(libc::SIGTERM);
                self.term_deadline = Some(now + STOP_GRACE);
            } else {
                // The leader is finished but something it started is not, and
                // nothing asked that leftover group to linger.
                self.kill();
                self.kill_deadline = Some(now + KILL_GRACE);
            }
        } else if self.term_sent
            && !self.kill_sent
            && self.term_deadline.map(|at| now >= at).unwrap_or(false)
        {
            self.kill();
            self.kill_deadline = Some(now + KILL_GRACE);
        } else if self.kill_sent && self.kill_deadline.map(|at| now >= at).unwrap_or(false) {
            return if self.stop_accepted {
                GuardState::Unconfirmed
            } else {
                GuardState::Abandoned
            };
        }
        if self.group.is_none() && self.leader_exited && self.kill_sent {
            // An identity that was never captured is never a confirmed stop.
            return if self.stop_accepted {
                GuardState::Unconfirmed
            } else {
                GuardState::Abandoned
            };
        }
        GuardState::Running
    }

    /// Whether any process of the owned group still exists.
    ///
    /// Absence is proven by exactly one answer — `ESRCH`, meaning no process in
    /// that group exists. Every other outcome, including any error this core
    /// cannot interpret, is not proof of absence and is reported as alive.
    pub fn group_alive(&self) -> bool {
        let Some(group) = self.group else {
            return false;
        };
        let result = unsafe { libc::killpg(group, 0) };
        if result == 0 {
            return true;
        }
        match io::Error::last_os_error().raw_os_error() {
            Some(libc::ESRCH) => false,
            // EPERM proves the group exists but is not ours to signal.
            Some(libc::EPERM) => true,
            _ => true,
        }
    }

    /// Signal the captured group, or nothing at all.
    fn signal(&self, number: i32) {
        let Some(group) = self.group else {
            return;
        };
        if group <= 1 || group == unsafe { libc::getpgrp() } {
            return;
        }
        unsafe {
            libc::killpg(group, number);
        }
    }

    fn kill(&mut self) {
        self.signal(libc::SIGKILL);
        self.kill_sent = true;
    }
}

impl Drop for ChildGuard {
    /// Dropping the guard closes the liveness channel and reaps the leader if
    /// it already exited. A live group is left for the owner to settle: this is
    /// not a stop, and it is never reported as one.
    fn drop(&mut self) {
        let _ = self.child.try_wait();
    }
}

/// The group id of a just-spawned group leader, or `None` if unproven.
fn group_of(pid: u32) -> Option<i32> {
    let group = unsafe { libc::getpgid(pid as i32) };
    if group < 0 {
        // The child already exited: nothing to cancel and nothing to confirm.
        return None;
    }
    if group == pid as i32 {
        Some(group)
    } else {
        None
    }
}

/// A pipe whose write end never reaches the child.
///
/// Both ends are created close-on-exec, and only the read end has that flag
/// cleared for the single spawn that follows. The child therefore inherits the
/// read end and never a copy of the write end, so the channel closes exactly
/// when this process stops holding it — including when it dies.
fn create_pipe() -> io::Result<(Option<RawFd>, Option<OwnedFd>)> {
    let mut fds = [0 as RawFd; 2];
    let created = unsafe { libc::pipe(fds.as_mut_ptr()) };
    if created != 0 {
        return Err(io::Error::last_os_error());
    }
    let (read_fd, write_fd) = (fds[0], fds[1]);
    for fd in [read_fd, write_fd] {
        unsafe {
            libc::fcntl(fd, libc::F_SETFD, libc::FD_CLOEXEC);
        }
    }
    unsafe {
        libc::fcntl(read_fd, libc::F_SETFD, 0);
    }
    Ok((
        Some(read_fd),
        Some(unsafe { OwnedFd::from_raw_fd(write_fd) }),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shell(script: &str) -> Vec<String> {
        vec!["/bin/sh".to_string(), "-c".to_string(), script.to_string()]
    }

    fn poll_until_settled(guard: &mut ChildGuard, stop: bool, bound: Duration) -> Vec<GuardState> {
        let deadline = Instant::now() + bound;
        let mut seen = Vec::new();
        loop {
            let state = guard.poll(stop);
            seen.push(state);
            if state != GuardState::Running || Instant::now() >= deadline {
                return seen;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    }

    /// Absence of one process group, proven by `ESRCH` and nothing else.
    fn group_is_absent(pgid: i32) -> bool {
        if unsafe { libc::killpg(pgid, 0) } == 0 {
            return false;
        }
        matches!(io::Error::last_os_error().raw_os_error(), Some(libc::ESRCH))
    }

    /// Owns the group a test child actually leads, so no test can finish with a
    /// live owned process — on success or on an assertion failure. The group id
    /// is captured *before* the production handle forgets it, and the guard is
    /// disarmed once absence is proven.
    struct TestGroup {
        pgid: i32,
    }

    impl TestGroup {
        fn of(guard: &ChildGuard) -> TestGroup {
            TestGroup {
                pgid: guard
                    .group
                    .expect("a live spawn must capture its group for the test to own"),
            }
        }

        /// Kill the owned group and wait until it is proven absent. The leader is
        /// reaped here, because a killed process that nobody reaps keeps its
        /// group non-empty and could never be shown gone.
        fn terminate(&self, guard: &mut ChildGuard) {
            unsafe {
                libc::killpg(self.pgid, libc::SIGKILL);
            }
            let deadline = Instant::now() + Duration::from_secs(5);
            while !group_is_absent(self.pgid) {
                let _ = guard.child.try_wait();
                assert!(
                    Instant::now() < deadline,
                    "the test's own child group never became absent"
                );
                std::thread::sleep(Duration::from_millis(10));
            }
        }

        fn disarm(mut self) {
            self.pgid = 0;
        }
    }

    impl Drop for TestGroup {
        fn drop(&mut self) {
            if self.pgid > 1 {
                unsafe {
                    libc::killpg(self.pgid, libc::SIGKILL);
                }
            }
        }
    }

    #[test]
    fn a_live_group_is_running_and_its_absence_is_proven_after_the_group_dies() {
        let mut guard = ChildGuard::spawn(&shell("sleep 30"), None, false).unwrap();
        let group = TestGroup::of(&guard);
        assert_eq!(group.pgid, guard.pid() as i32);
        assert_eq!(guard.poll(false), GuardState::Running);
        assert!(guard.group_alive());

        // Kill the whole captured group; absence is then ESRCH, and only that.
        guard.signal(libc::SIGKILL);
        let states = poll_until_settled(&mut guard, false, Duration::from_secs(5));
        assert_eq!(
            states.last(),
            Some(&GuardState::Stopped),
            "a group proven absent must settle: {states:?}"
        );
        assert!(!guard.group_alive());
        group.disarm();
    }

    #[test]
    fn an_unrecaptured_group_identity_is_never_a_confirmed_stop() {
        let mut guard = ChildGuard::spawn(&shell("sleep 30"), None, false).unwrap();
        // The spawn race this guards against: a live child whose group identity
        // was never captured. The test keeps its own knowledge of the group it
        // really started, so it can always end that process.
        let group = TestGroup::of(&guard);
        guard.group = None;
        let states = poll_until_settled(&mut guard, false, Duration::from_secs(2));
        assert!(
            !states.contains(&GuardState::Stopped),
            "an unknown group identity must never be reported stopped: {states:?}"
        );
        group.terminate(&mut guard);
        group.disarm();
    }

    #[test]
    fn a_reap_this_core_could_not_complete_is_never_a_confirmed_stop() {
        let mut guard = ChildGuard::spawn(&shell("sleep 30"), None, false).unwrap();
        let group = TestGroup::of(&guard);
        group.terminate(&mut guard);
        // The group is gone, but the reap this core attempted did not complete.
        guard.leader_exited = true;
        guard.reap_uncertain = true;
        let states = poll_until_settled(&mut guard, true, Duration::from_secs(2));
        assert!(
            !states.contains(&GuardState::Stopped),
            "an unproven reap must never be reported stopped: {states:?}"
        );
        group.disarm();
    }

    #[test]
    fn an_exited_process_has_no_captured_group() {
        let mut child = std::process::Command::new("/bin/sh")
            .args(["-c", "exit 0"])
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .unwrap();
        child.wait().unwrap();
        assert_eq!(group_of(child.id()), None);
    }
}
