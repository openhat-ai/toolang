# Flat Name Encoding (FN Encoding)

Status: approved in conversation for implementation as a standalone common
module, including this document and acceptance tests in one PR. Caller
integration and storage migration remain outside scope.

## Goal and scope

Encode arbitrary strings reversibly into readable ASCII intended for filename
components on modern Windows, macOS, and Linux filesystems. Escape prohibited
filename characters, but leave Windows reserved names unchanged. Length and
reserved-name admissibility are outside the codec's guarantee. Common ASCII
URL and path characters should remain recognizable. Preserve case: `Foo`
encodes as `Foo`, and `foo` as `foo`. Different inputs need not coexist on a
filesystem that compares names without regard to case.

The codec processes strings without parsing or validating URL/path syntax. It
does not resolve paths, decode URL escapes, normalize Unicode, access the
filesystem, or change existing storage layouts. It performs neither device-name
checks nor filesystem validation.

## Encoding contract

Use one deterministic encoder and a single-pass decoder:

| Input | Encoded form |
| --- | --- |
| `/` | `%` |
| `%` | `+25` |
| `+` | `++` |
| Other escaped byte | `+hh`, exactly two lowercase hexadecimal digits |
| Empty string | `+_`, a token recognized only as the entire encoded string |

Preserve printable ASCII characters from U+0021 through U+007E except the
special characters above and `<>:"\|?*`. Encode those excluded characters,
ASCII spaces, controls, DEL, and all non-ASCII characters as bytes. This keeps
letters, digits, case, dots, and punctuation such as `@`, `&`, `=`, and `#`
readable while ensuring that output contains only ASCII.

Use UTF-8 for byte escapes. To cover every Python `str`, including isolated
surrogates, use the symmetric `surrogatepass` extension when converting between
strings and bytes. This changes no encoding of ordinary Unicode text.

Additional encoding rules:

- Encode every dot in the final run of dots as `+2e`. Consequently `.` and `..`
  never remain directory-navigation names. Internal and leading dots remain.
- Do not impose a length limit, perform length checks, truncate, substitute a
  hash, or create an external mapping. Return the complete encoded string.

There are no unconditional prefixes or suffixes. The input `+_` becomes `++_`,
so it cannot collide with the empty-string token.

## Decoding contract

Recognize the whole-string empty token first. Otherwise scan left to right:
`%` contributes byte `2f`, `++` contributes `2b`, `+hh` contributes one byte,
and literal ASCII contributes its own byte. Decode the accumulated bytes using
UTF-8 with `surrogatepass`. Never decode the resulting string a second time.

Accept either hex letter case and escaped forms of otherwise literal bytes;
the encoder always emits the canonical form above. Reject incomplete or
unknown plus escapes, raw non-ASCII, and malformed byte sequences with
`ValueError`. This is codec syntax checking, not URL/path or filesystem
validation. Do not consult the local operating system during decoding.

For every input string, `decode(encode(s)) == s`, including case, original URL
percent escapes, slash direction, and controls. Noncanonical encoded spellings
may decode to the same string.

## Examples

| Input | Canonical filename |
| --- | --- |
| `Foo` / `foo` | `Foo` / `foo`, unchanged |
| `/usr/local/bin` | `%usr%local%bin` |
| `https://example.com/a/b?q=1&x=2#top` | `https+3a%%example.com%a%b+3fq=1&x=2#top` |
| `a%2Fb` | `a+252Fb` |
| `a+b` | `a++b` |
| `+2f` | `++2f` |
| `C:\Users\Foo` | `C+3a+5cUsers+5cFoo` |
| `my file.txt` | `my+20file.txt` |
| `CON` | `CON` |
| `nul` | `nul` |
| `CON.txt` | `CON.txt` |
| `con.` | `con+2e` |
| `..` | `+2e+2e` |
| Empty string | `+_` |
| U+4E2D U+6587 | `+e4+b8+ad+e6+96+87` |
| U+0000 | `+00` |

## Implementation touchpoints

Current agent naming in `src/toolang/common/layout.py` derives constrained,
non-reversible identifiers; acquisition keys in `src/toolang/common/source.py`
are hashes. Neither is a substitute for this codec or an approved integration
point. Existing URL quoting in source/workspace helpers retains its URI role.

Implementation files are `src/toolang/common/flatname.py` and
`tests/unit/common/test_flatname.py`, exposing `encode_flatname` and
`decode_flatname`. Keep the functions pure, use the standard library, and avoid
expanding package facades or adding CLI flags.
Select a caller and any storage migration in a separate approved definition.

## Acceptance

- Round-trip all individual ASCII characters, representative Unicode and
  isolated surrogates, empty input, controls, and mixed URL/path strings.
- Match the example table, preserving `Foo` and `foo` exactly.
- Distinguish `/`, `%`, `+`, `+2f`, literal URL escapes, and backslashes; escaped
  output must never be decoded recursively.
- Preserve Windows reserved names with and without extensions and mixed case;
  cover trailing dot runs, spaces, `.` and `..`.
- Output is nonempty ASCII, has no forbidden filename characters or controls,
  and never ends with a space or dot.
- Round-trip strings producing outputs beyond common filesystem length limits
  without codec errors or truncation. All unit checks are deterministic and
  offline.
- Reject malformed decoder tokens/bytes while accepting uppercase hexadecimal
  and valid noncanonical escapes.
- Run the repository's default verification before committing implementation.

## Limits and risks

Every encoded result avoids the specified prohibited filename characters.
Names such as `CON` and `nul` remain unchanged and may be rejected by Windows.
The codec accepts strings of arbitrary length; an actual filesystem operation
may also fail because of component or total-path length. File-writing callers
own any admissibility checks and filesystem errors.

The character-escaping guarantee does not cover reserved names, length,
directory permissions, existing entries, legacy 8.3 filesystems, or
service-specific naming policies. Case-insensitive collisions remain accepted
by the human. ASCII output avoids Unicode normalization changes, at the cost of
longer and less readable non-ASCII text.
The fixed codec rules require versioned ownership if persisted later.

References: [Windows naming rules](https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file),
[reserved names](https://docs.python.org/3/library/os.path.html#os.path.isreserved),
[filesystem length limits](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-getvolumeinformationw),
and [Apple normalization behavior](https://developer.apple.com/library/archive/documentation/FileManagement/Conceptual/APFS_Guide/FAQ/FAQ.html).
