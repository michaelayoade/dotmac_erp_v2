# Staff identity enrolment

This operator-only path preserves selected active local staff identity facts.
It does not reset passwords, migrate sessions/MFA/reset tokens, infer legacy
administrators, or enable production traffic. Staff receives only
`erp.web.access`; financial permissions remain separately controlled.

Legacy Passlib PBKDF2 uses an envelope/base64 alphabet different from released
kernel a107. Conversion preserves exact rounds, raw salt and digest; no plaintext
password is required. Bcrypt and malformed/unsupported encodings block selected
accounts. Successful first login upgrades supported PBKDF2 to Argon2 through the
existing kernel service. A subsequent enrolment refuses to restore an old source
hash over that changed target credential.

Use a private operator directory, environment-only read-only source and migration
owner DSNs, an independently verified restored snapshot, and an explicit UUID
allowlist. Source credentials/hashes must never enter Git, logs, receipts or CI
artifacts. Plan receipts contain counts/codes and input-file digests only.

The allowlist is JSON with version=1, tenant_id, snapshot_sha256,
candidate_revision (full revision matching the manifest), and mappings. Each
mapping names credential_id, person_id, party_id and employee_id. The person and
Party UUIDs must match; destination employee, person and tenant are independently
verified. Do not choose a winner among duplicate emails or identities.

Planning (read-only source; destination rows may be locked, never changed):

```
python -m erp_v2.importer.identity_cli \
  --manifest /private/manifest.json --snapshot-file /private/snapshot.dump \
  --allowlist /private/staff-allowlist.json --receipt /private/identity-plan.json
```

Both source/destination observed database names must match the manifest, and the
snapshot file checksum must match. A separate restore receipt must establish that
the source database actually came from those snapshot bytes; filenames and DB
names alone cannot establish that provenance. Serving DATABASE_URL materials must
be absent from the operator process. The running image/revision also needs an
independent release binding; echoing a manifest revision is not artifact proof.

Apply additionally requires ERP_IDENTITY_IMPORT_APPROVED=1, --apply and
--approved-allowlist-sha256 and --approved-manifest-sha256 with the exact
digests from the reviewed private plan.
The entire allowlist is revalidated in one caller-owned destination transaction;
a private process-local plan binding prevents same-count input substitution.
Selected blocked rows refuse the whole apply; unselected rows remain preserved
outside the cohort. Nothing creates admin grants or alters existing grants.

Receipts are exclusively created mode0600 before database work. Existing evidence
blocks a run instead of being overwritten. On database/transport/commit uncertainty,
preserve the receipt and destination; do not assume no write happened. Reconcile
and use a fresh receipt path for a controlled replay. Exact unchanged credentials
are idempotent; changed credentials are refused rather than overwritten.

Gate5 still needs a designated existing staff member to enter their own password
in the application against the rebuilt test destination, then prove permitted
workspace access and denied financial writes. Synthetic fixture login is not that
real-account proof. No passwords or hashes belong in the proof receipt.

Source eligibility is independently checked against the legacy person's
organization, active flag and active status. Reset-required, currently locked,
unknown lock state, outstanding failed-attempt state, or enabled MFA rows block
the cohort; no MFA secret is read or copied and no security flag is cleared.
These account states need their supported source/target flow, not an automatic
migration override. The importer is not proof of complete authentication-policy
parity (including future lockout and MFA operations); that remains a release gate.

The legacy source schema is fixed to `public`; no unreviewed schema override
can change which credential, person or MFA tables supply a reviewed cohort.
