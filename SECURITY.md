# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Use GitHub's private vulnerability reporting:
[Report a vulnerability](https://github.com/saitokiku/field-deck/security/advisories/new).

Please include:

- What an attacker can do, and what access they need to start
- Steps to reproduce, ideally against the simulated bench so anyone can run them
- The version or commit
- Whether it is already public anywhere

You should get an acknowledgement within a week. This is a small project run by
people with day jobs; that is a realistic commitment rather than an ambitious
one. If a week passes with no reply, please ping the issue tracker without
details — a "sent you a security report" comment is fine.

---

## What counts as a vulnerability here

FieldDeck's threat model is unusual, so it is worth being explicit.

### In scope, and taken seriously

**Anything that lets a client act on hardware without a human authorizing it.**
This is the core promise. Concretely:

- A path by which an action runs without an active grant of its exact
  permission class
- A way for a **recipe**, the **MCP server**, or any non-human client to create
  a grant
- A way to bypass a configured **limit** with any authorization
- A way to act on hardware while an **emergency stop is latched**, other than
  moving toward safety
- A way to make a **lease** not release on client death, expiry or daemon exit
- A way to reach `safety.arm`, `safety.disarm`, `safety.estop_clear` or
  `safety.lease_renew` from the restricted AI socket
- A way to have a request recorded with a **`source` other than the socket it
  arrived on**

Two examples of real findings in this class, both fixed before the first
release, to calibrate what we mean:

> `allowed_during_estop` was read off the `ActionSpec` rather than the resolved
> permission, so the flag that let you *de-energise* a rail during a latched
> stop also let you *energise* one. It was masked by a policy default rather
> than prevented by design.

> Starting a second daemon silently stole the control socket, leaving two
> processes each believing they owned the hardware — and an emergency stop
> reaching only one of them.

**Also in scope:**

- Privilege escalation from the `fielddeck` user
- Arbitrary command or code execution through RPC, MCP, a recipe or a config
  file — including anything that gets a shell out of the expression evaluator
- Path traversal out of the session store or config directory
- Credentials or keys appearing in logs, events or session artifacts
- Remote reachability of the control API when it is configured not to be
- Denial of service that a *remote or unprivileged* party can trigger

### Out of scope

- **Anything requiring root on the Pi.** Root already owns the daemon.
- **Physical access.** Someone at the bench can unplug things.
- **A human arming a permission and then breaking their own hardware.** That is
  the system working; the operator is the authority and FieldDeck's job is to
  make sure it was a decision.
- **Deliberately exposing the control API to a network** and being reached over
  it. The config model rejects a wildcard bind and the documentation says not
  to; overriding both is a configuration choice, not a vulnerability.
- **A wrong conclusion from the analyzer or an assistant.** FieldDeck guarantees
  that a wrong conclusion cannot become a wrong *action* without a human. It
  does not guarantee the conclusion is right. Do report analyzers that are
  *confidently* wrong where they should say "unknown" — that is a real bug,
  just not a security one.
- **Missing hardware verification.** Every instrument profile ships
  `hardware_verified: false` and this is stated in the README. A profile that
  is wrong about a SCPI dialect is a bug report, not an advisory.

---

## Deployment guidance

**Never expose the control socket to a network.** The control API has no
authentication because it does not need any: it is a Unix socket, and filesystem
permissions are the access control. The optional remote block is off by default,
binds to `127.0.0.1`, and the config model rejects a wildcard bind outright. If
you need remote access, use SSH port forwarding — an SSH tunnel is
authenticated, and the API is not.

**Group membership is the access control.** Anyone in the `fielddeck` group can
arm any permission class the policy allows. Treat it like `sudo` access to the
bench.

**Config is root-owned on purpose.** The daemon reads its safety policy and
cannot rewrite it. A daemon that can raise its own limits does not have limits.
If you change that ownership, you have removed a control.

**Run the MCP server against the restricted socket** — it defaults there. Set
`FIELDDECK_AI_GROUP` and run it as a user in only that group to make the AI
boundary kernel-enforced rather than a matter of client configuration.

**Sessions may contain sensitive data.** A capture is a recording of whatever
was on the wire, which may include credentials, keys, or a customer's
proprietary protocol. `/var/lib/fielddeck` is mode 0750. The uninstaller keeps
sessions deliberately; delete them yourself when you mean to.

---

## Supported versions

Pre-1.0. Only the latest release gets fixes. Once there is a 1.0 this section
will say something more useful.

## Disclosure

We will credit you unless you prefer otherwise, agree a disclosure date with
you, and publish an advisory when a fix ships. If a report is out of scope we
will say so plainly and explain why rather than leaving it unanswered.
