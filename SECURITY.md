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

<a id="what-a-leaked-api-token-gets-an-attacker"></a>
## What a leaked API token gets an attacker

Worth stating plainly, because "a token" is not one thing here and a report
should be able to say which one it means.

A **read-only** token is an information disclosure bounded by one account:
the feed as that user sees it, their topic and source selections, their
bookmarks, and their reading history. It cannot change anything, cannot
delete the account, and cannot see another user's state.

A **full-access** token is that account. Everything the signed-in user could
do through the browser, including `DELETE /api/v1/me`. Treat a leaked one as
a session compromise.

Neither can mint or revoke tokens. `/api/v1/me/tokens` requires the browser
session and answers 403 to a bearer credential however privileged, which is
what makes revocation end the access rather than merely inconvenience it —
otherwise the holder of a leaked full token could have issued themselves a
replacement before anybody noticed. Neither can sign in, either: there is no
route that turns a token into a session.

What is *not* claimed. There is no per-token audit trail — `last_used_at`
says a token is in service and nothing about what it did — and failed token
authentication is not rate limited, deliberately, on the reasoning in
[ROADMAP.md](ROADMAP.md#scaling). A report showing either of those absences
being exploited is a useful report; a report that they are absent is
answered by this paragraph.

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
- **API tokens** are the second credential and are stored the same way — a
  SHA-256 digest only, 256 bits of `secrets` entropy behind a fixed
  `sretab_pat_` prefix, shown once at creation and never retrievable. They
  carry one of two scopes, and a read-only token is refused on every mutating
  method by middleware rather than by any route's own check. The allow-list
  is re-checked on every token request, so removing an account from
  `ALLOWED_GITHUB_IDS` kills its tokens immediately. CSRF is enforced exactly
  when the session cookie is present, so a bearer request is exempt and a
  browser request carrying both credentials is not.
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
