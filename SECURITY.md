# Security policy

## Reporting a vulnerability

Report privately through GitHub's [security advisory
form](https://github.com/Darkflib/sre-tab/security/advisories/new). That
opens a channel visible only to you and the maintainers, which is the point
— a public issue is a disclosure, and this repository has no embargo
machinery beyond it.

Please do not open a public issue for a suspected vulnerability.

Expect an acknowledgement within seven days. This is a single-maintainer
project with no on-call rota, so that is a realistic figure rather than an
aspirational one; if a week passes with no reply, the acknowledgement is
what has failed, and a nudge on the same advisory thread is welcome.

Useful in a report, roughly in order of value: what an attacker gains, the
smallest sequence of steps that demonstrates it, and the commit or release
you tested. A proof of concept against your own instance is worth more than
a description of one. If a finding depends on a deployment shape different
from the one described below, say which — that is usually the whole
question.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x | Yes |
| < 1.0 | No — pre-release, superseded by 1.0.0 |

Fixes land on `main` and are released from there. There is no long-term
support branch.

## Scope

In scope is anything in this repository: the application in `app/`, the
client in `frontend/`, the deployment units and scripts in `deploy/`, the
container build, and the CI workflows — including the supply-chain
machinery, since a weakness in signing or promotion is a weakness in what
gets run.

Out of scope: findings against a third-party feed's own infrastructure,
denial of service by volume against an instance you control, and anything
that requires an operator account to exploit — see the next section for why
that last one is a judgement rather than a dismissal.

## Assumptions this deployment makes

Several accepted findings are held open on the *shape* of the reference
deployment rather than on a judgement that the code is right: one instance,
a small allow-list of operators, an operator-curated source catalogue, and
no route that lets a user add a feed. They are written up in full in
[ROADMAP.md](ROADMAP.md#security-findings-this-deployment-absorbs), with the
reasoning for each and the condition that would change its severity.

Reading that section first is worth the five minutes. It will tell you
whether something you have found is already known and deliberately held, and
if it is, the more useful report is usually the one that shows the stated
assumption does not hold.

## What the project already does

So a report can skip ground that is covered, and so a gap in any of it is
recognisable as a finding:

- **Ingest** is RSS and Atom only, behind an SSRF guard: https on every hop
  including redirects, DNS resolved with private, link-local, and reserved
  ranges refused, a response-size cap counted in wire bytes, short timeouts,
  and summaries sanitised to text rather than rendered as feed HTML.
- **Sessions** are an `HttpOnly`, `Secure`, `SameSite=Lax` cookie; only a
  SHA-256 digest of the token is stored. Mutating routes require a signed
  double-submit CSRF token bound to the session it was issued for.
- **Sign-in** is GitHub OAuth, entirely server-side, against an allow-list
  of numeric GitHub IDs. There is no public sign-up.
- **The supply chain** is pinned end to end: actions by commit SHA, base and
  application images by digest. Published images are cosign-signed against
  GitHub's OIDC identity and carry SLSA provenance and an SPDX SBOM.
  Verification happens at publish, at promotion, on every CI run, and on
  demand via `deploy/scripts/verify-image.sh` — but **not** at container
  start, for the reason set out in ROADMAP.md.
- **CI** runs Bandit, Semgrep, `pip-audit`, and `npm audit` as gates, with
  the Semgrep run guarded so that a scan which errored or covered no files
  fails the build rather than passing with no findings.

## Credit

Reporters are credited in the advisory and in `CHANGELOG.md` unless you ask
otherwise. There is no bounty — this is a self-hosted side project, and
pretending otherwise would waste your time.
