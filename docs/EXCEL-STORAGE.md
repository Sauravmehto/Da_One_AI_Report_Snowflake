# Excel report storage

One bucket, three prefixes. The prefixes are not cosmetic — each has a
different retention rule, and the lifecycle policy keys off them.

| prefix | what it holds | retention |
|---|---|---|
| `uploads/` | a scanned workbook that has not been approved yet | expires after 7 days |
| `templates/` | the approved workbook a report is built from | **never expires**, versioned |
| `reports/` | one immutable file per run | kept; cooled after 90 days |
| `plans/` | a proposed dashboard or previewed edit, between the propose and save steps | expires after 2 days |

`plans/` is not about Excel at all — it is here because the same bucket solves
the same problem. Propose-then-save flows held their proposal in a module-level
dict, which is one worker's memory, so behind `--workers 4` the save landed on a
process that had never seen the proposal and reported it as expired seconds
after it was made.

## Why three, not two

`templates/` is why an incoming/outgoing split does not work. It arrives as an
upload, so it looks like "incoming" — but it is the source of truth that every
future run reads from, forever. A bucket holding it can never be treated as a
landing zone or given an expiry, which is the whole point of having one.

## Why generated reports are stored rather than regenerated

The query is parameterised by date, but the table underneath is not frozen:
late-arriving records, corrections and restatements all change history. Running
January's report again in March produces **different numbers for the same
as_of_date**. If a report has gone to an investor, "what exactly did we send
them" has to be answerable, and only the stored file answers it.

Regenerating on download would also re-run every query — tens of seconds and
warehouse cost — to reproduce a file that already existed.

## Setting it up

There are no folders to create. S3 has no directories — `uploads/`,
`templates/` and `reports/` are ordinary key prefixes that come into existence
when the app writes its first object. Create the bucket; the layout follows.

**The lifecycle file is not a bucket policy.** They are different things and the
console keeps them on different tabs: lifecycle lives under **Management →
Lifecycle rules**, while **Permissions → Bucket policy** wants IAM-shaped JSON
(`Version` + `Statement`) and will reject `Rules` outright. Pasting one into the
other's editor fails with "Invalid Policy Element".

This bucket needs no bucket policy at all. Grant access on the IAM role the
container runs as (see [Access](#access)); a bucket policy is for cross-account
sharing or explicit denies, neither of which applies here.

```sh
aws s3api create-bucket --bucket dataalphaone-excel-storage \
  --region <region> --create-bucket-configuration LocationConstraint=<region>

aws s3api put-public-access-block --bucket dataalphaone-excel-storage \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

aws s3api put-bucket-versioning --bucket dataalphaone-excel-storage \
  --versioning-configuration Status=Enabled

aws s3api put-bucket-encryption --bucket dataalphaone-excel-storage \
  --server-side-encryption-configuration \
  '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"},"BucketKeyEnabled":true}]}'

aws s3api put-bucket-lifecycle-configuration --bucket dataalphaone-excel-storage \
  --lifecycle-configuration file://docs/s3-lifecycle.json
```

Then point the backend at it:

```
EXCEL_STORAGE_BACKEND=s3
EXCEL_S3_BUCKET=dataalphaone-excel-storage
EXCEL_S3_PREFIX=
```

`EXCEL_S3_PREFIX` is how one bucket serves several deployments or clients — set
it to `staging`, or to a client name, and every key lands underneath it. Leave
it empty for a single deployment.

## Access

Granted on the IAM role the container assumes, not on the bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::<bucket>/*"
    },
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::<bucket>"
    }
  ]
}
```

`ListBucket` is on the bucket ARN, the object actions on `/*` — a common
stumble, and the symptom is a listing that returns nothing while individual
downloads work.

Nothing outside the app reads this bucket: downloads stream through the backend
rather than being handed out as presigned URLs, so the session check cannot be
bypassed. That is also why there is no public or cross-account access to set up.

## What is NOT yet solved

Switching to S3 makes the *files* visible to every worker. It does not fix
`_SCAN_CACHE`, which is still an in-process dict holding the scan metadata
between upload and configure. Until that moves to Redis, the Report Builder only
works when the same worker serves both requests — so it stays behind
`VITE_SHOW_REPORT_BUILDER` in any multi-worker deployment.

Objects also move through the backend whole: the engine returns the workbook as
bytes and downloads do `Response(content=store.get(key))`. Fine at the measured
sizes (100,000 rows is a 9.4MB file), but it is memory per concurrent request,
not a stream.
