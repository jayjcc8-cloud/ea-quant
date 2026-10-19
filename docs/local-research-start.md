# Start research in QUINE desktop

The Mac desktop is the sole user entry point. The current Apple Silicon macOS application is an
unpublished local candidate, not a public download. Open the explicitly supplied app normally;
do not install or start a browser service. [Desktop setup](desktop-macos.md) covers the candidate,
workspace and signing limitations; [first-use acceptance](first-use-acceptance.md) records what
you actually verify in the installed application.

## Workspace and model configuration

Check **Settings → Workspace** before importing data. Ordinary launches use
`~/Library/Application Support/QUINE`; a controlled candidate may explicitly use an isolated root.
To select another workspace, save an existing empty directory or existing QUINE workspace, wait
for running work to finish, quit normally and reopen. Saving selects the next location; it does
not move data or make a second writer. Cancel abandons unsaved input. Never copy frozen receipts
to another root to bypass identity checks.

Save an idea, inspect resources and run a valid local experiment without configuring a paid model.
Use **Resources → Model connections** only if you choose AI assistance. A saved connection is not
proof of authentication. It makes no provider call until an explicit model action; absent
configuration must remain visible. Keys stay in the backend-owned settings store described in
[desktop configuration](desktop-macos.md), never in WebView storage. No terminal key prompt or
project `.env` is part of the desktop startup path.

## One research path

Save the idea in **Research**, import data through **Resources → Data → Import CSV**, and bind the
intended immutable data version to the intended draft. Review instrument, interval, sample
nature, initial funding/currency and assumptions. AI proposals show their target/version and
diff; applying one only edits the draft. Preview and explicitly confirm an experiment separately.
Read the frozen formal report, then refresh the same request without resubmitting it.

Missing data, an empty result and an expired result are different states. Do not substitute demo
values. An accepted research candidate does not authorize simulation or Live; simulation requires
its own explicit confirmation with the accepted source report and frozen inputs. Live stays an
unconnected preview. Unknown requests are queried using their original identity, never blindly
resent.

## Guided upload of your own CSV

The controlled data upload panel has two editors over one declaration.

**Guided.** Choose the upload mode "Explicit mapping of my own file", then choose a destination for
each of the strict profile's fifteen fields. The column choices are your file's own header row and
nothing else: no data row is read and no value is inferred. The six metadata fields may instead be
a constant you state; the three timestamp fields each name one exact form from this host's closed
allowlist, and the time zone is stated by you, never guessed. Nothing is pre-filled.

**Advanced.** The same declaration is editable as JSON, and choosing that editor ("Advanced JSON")
is explicit. There is one declaration, not two: the guided controls are read from the JSON text and
write back into it, so a column stated as JSON is the column the controls show, a guided change
never reverts it, and neither editor can overwrite the other. A declaration the controls cannot
represent — a destination outside the strict profile, a constant on a field that carries an
observation, a timestamp form outside the allowlist, a field named as both a column and a constant
— keeps the JSON editor and shows the reason, instead of dropping or re-pointing anything. Under
the simulated policy the availability destination is shown as derived and is not an editable value
the panel would then ignore.

What the panel does not do, deliberately:

- A header that repeats a name or leaves a column unnamed yields no column choices at all, with
  the reason shown: no name is invented for an unnamed column.
- A header with a quote inside an unquoted name (`clo"se"`) or with characters right after a
  closing quote (`"close"x`) is refused as a whole, again with no column choices: reading either one
  as a name would offer a column the file does not have. Quoted commas, doubled quotes and CRLF
  endings are read as the file writes them.
- A byte-order mark is never silently stripped from a name. The names are read as if it were not
  there, the mark is stated on screen, and the bytes sent are still your file's own — so the host
  refuses the source (`invalid_encoding`) rather than the page repairing it.
- A destination chosen for a column your next file no longer has stays exactly as you chose it, is
  marked "not in this file", and is still sent. The host refuses it (`mapping_column_missing`);
  nothing is re-pointed for you.
- Under the simulated availability policy no availability column or form is declared, because the
  policy derives availability itself and declaring one would contradict it.
- Changing the file, any choice, the time declaration or the policy clears the preview. A version
  is published only from a preview of the exact declaration on screen that accepted the whole
  source; the host is the only authority on whether a declaration is acceptable.
- A pasted source is sent as the text the panel holds, which is what a textarea's own value is:
  pasting CRLF text gives LF there. A file selected with the file control is sent as that file's own
  bytes, base64-encoded, so its line endings, its mark and its quoted header reach the host exactly
  as written.

A published version is never reported as verified point-in-time availability (`pit_verified` is
always false for an upload), and the source and licence strings are your assertion, not a legal
verification.

## Evidence and internal previews

Synthetic examples and scripted previews are useful checks, not market evidence or proof of a
successful model request. Record any real-data or native interaction path you have not performed
as unverified. The [contributor guide](../CONTRIBUTING.md#internal-ui-and-service-checks) contains
the internal fixture preview; it is not another user startup or acceptance path.
