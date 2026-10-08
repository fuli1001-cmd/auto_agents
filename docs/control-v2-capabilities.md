# Business control v2 capability and retirement matrix

The controller and CLI cutover are implemented. The final regression, migration audit and bounded native acceptance are recorded in [control-v2-acceptance.md](control-v2-acceptance.md); native scope limitations remain explicit.

| Capability | Controller boundary | Acceptance |
|---|---|---|
| run stages | fixed stage catalog and work contracts | real run, stage artifacts and approvals |
| fix | classification, implementation, verification, independent review, delivery | exact command preserved, real fix |
| collab | diagnosis, owned child work, original goal acceptance | full child return and semantic evidence |
| provider resolution | artifact-scoped research work | primary sources and unchanged accounts |
| approval / reject / answer | versioned state transition | no model cost or budget reset |
| task dependencies / parallel work | controller ready queue; private workspaces | conflicts serialized; merged results reverified |
| verification / caching | frozen specs, source/environment identities | no empty, skipped or unexecuted proof |
| requirements / persistence / prototype | domain validators; ownership stays with controller | immutable approvals and explicit migration authorization |
| unknown external result | durable operation receipt | reconcile before redispatch, including provider change |
| snapshots / recovery | schema-2 public API | exact source/contract/budget preserved |
| resource collection | explicit owners, references and process leases | files, DB and Docker; no user resources removed |
| old execution retirement | no calls into old run/session/workflow drivers | dependency/static audit and public behavior tests |

A compatibility entrypoint may delegate to the new controller. It must not call an old executor, old resume normalization or legacy workflow mutation.
