# Source Diagnostic Examples

Verified `too parse --check - --stdin-filepath case.too` outputs. Inputs use
escaped `\n` and `\t` to make whitespace and missing final newlines visible.
Every case exits with status 1 and no stdout.

| Case | Input | Stderr |
| --- | --- | --- |
| Extra header text | `'\n\n\nagic issue(_: Text) sdf:\n  pass\n'` | `case.too:4:1: Parse error in agic block: 'agic issue(_: Text) sdf:\n  pass'` |
| Missing closing parenthesis | `'flow work(value: Text:\n  pass\n'` | `case.too:1:22: Expected ')' in parameter list: 'flow work(value: Text:'` |
| Missing field type | `'struct X:\n  field:\n'` | `case.too:2:9: Expected a field type: 'field:'` |
| Missing parameter type | `'flow work(value: ):\n  pass\n'` | `case.too:1:16: Parse error in parameter list: ':'` |
| Empty property | `'skill review:\n  description =\n  Review.\n'` | `case.too:2:16: Property 'description' in skill 'review' must be nonempty: 'description ='` |
| Malformed flow statement | `'flow work:\n  sort these items\n'` | `case.too:2:3: Malformed flow statement 'sort': 'sort these items'` |
| Malformed message header | `'agic review:\n  user, broken\n'` | `case.too:2:3: Malformed message header 'user': 'user, broken'` |
| Missing repeat body | `'flow work:\n  repeat 2 times:\n  run: Review.\n'` | `case.too:1:1: Parse error in flow block: 'flow work:\n  repeat 2 times:\n  run: Review.'` |
| Incomplete header | `'flow work'` | `case.too:1:1: Parse error in flow block: 'flow work'` |
| Missing body | `'flow work:'` | `case.too:1:1: Parse error in flow block: 'flow work:'` |
| Unknown token | `'@\n'` | `case.too:1:1: Parse error: '@'` |
| Unicode query | `'flow work:\n  sort {{#中文}}\n'` | `case.too:2:3: Malformed flow statement 'sort': 'sort {{#中文}}'` |
| Multiple errors | `'@\nflow work(value: Text:\n  pass\n'` | `case.too:1:1: Parse error: '@'` |
| Field type at EOF | `'struct X:\n\tfield:'` | `case.too:2:8: Expected a field type: 'field:'` |

`fmt` and `fmt --highlight --html` report the same syntax evidence. For the
empty-property case they report `Expected a property value after '='`; AST
validation retains the property and capability names.

`parse --cst` retains every native error, including overlapping recovery regions.
For the missing-repeat-body case it additionally reports the nested recovery
at `3:3`: `Parse error in flow block: 'run: Review.'`. This is a raw parser
region, not an assertion that the `run` statement itself is malformed. For the
multiple-error case CST also reports the missing parenthesis on line 2.
Without a final newline, the field-type-at-EOF case produces a covering struct
recovery region in raw CST instead of the missing-type node produced by AST/fmt.

Independent `highlight` succeeds for all these inputs without stderr. It does
not perform syntax validation.

Duplicate properties retain both positions:

```text
conflict.too:3:3: Duplicate property 'description' in skill 'review': 'description = Second.'
conflict.too:2: note: Previous property
```
