#!/bin/sh
# Prepare MinIO for the media service: one bucket, one scoped service account.
#
# Runs once at `docker compose up` and exits. Idempotent throughout, because it runs again on
# every restart of the stack and must not fail the second time.
#
# The point of the service account is that the media service never holds the root credentials.
# A leaked key that can read and write one bucket is a different incident from one that can
# delete every bucket and rewrite policy — and the root user is also what the console logs in
# with, so sharing it would mean every service holding the admin password.
set -eu

ALIAS=arcadia
BUCKET="${MEDIA_S3_BUCKET:-arcadia-media}"
POLICY="arcadia-media-rw"

echo "waiting for minio to answer"
# `mc ready` waits for the drives to be formatted, not just the port to open. A service that
# connected in that window would fail its first request against a server that looks up.
mc ready --insecure "$ALIAS" >/dev/null 2>&1 || true
mc alias set "$ALIAS" http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc ready "$ALIAS"

echo "ensuring bucket $BUCKET"
# Prints "Bucket created successfully" even when it already existed, which is mc being
# misleading rather than this script recreating anything. `--ignore-existing` is what makes the
# exit code right, and the exit code is what `set -e` reads.
mc mb --ignore-existing "$ALIAS/$BUCKET"

# Private by default, then opened for anonymous *download* of public art.
# GetObject only — ListBucket stays denied — so a game build is still unguessable
# without its object key. Storefront covers are fetched by the browser from this
# host (`minio.arcadia.aptcodegen.online`) rather than through media-service.
mc anonymous set download "$ALIAS/$BUCKET" >/dev/null

# Browser `<img>` tags do not need CORS, but canvas / fetch do. Locked to the
# storefront origins rather than `*`.
cat >/tmp/cors.json <<CORS
{
  "CORSRules": [
    {
      "AllowedOrigins": ["https://arcadia.aptcodegen.online", "http://localhost:3000"],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedHeaders": ["*"],
      "ExposeHeaders": ["ETag", "Content-Length", "Content-Type"],
      "MaxAgeSeconds": 3600
    }
  ]
}
CORS
mc cors set "$ALIAS/$BUCKET" /tmp/cors.json >/dev/null

echo "ensuring the $POLICY policy"
cat >/tmp/policy.json <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ObjectsInOneBucket",
      "Effect": "Allow",
      "Action": [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts"
      ],
      "Resource": ["arn:aws:s3:::${BUCKET}/*"]
    },
    {
      "Sid": "ListAndMultipartOnTheBucketItself",
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:ListBucketMultipartUploads",
        "s3:GetBucketLocation"
      ],
      "Resource": ["arn:aws:s3:::${BUCKET}"]
    }
  ]
}
JSON
# Deliberately absent from that policy: CreateBucket, DeleteBucket, and anything on another
# bucket's ARN. The service is given S3_CREATE_BUCKET=false to match, so a typo in S3_BUCKET
# fails at boot instead of quietly starting a second, empty store.
# `create` overwrites an existing policy and exits 0, so this needs no existence check. Checked
# against this mc release rather than assumed: there is no `policy update` — it was removed, and
# calling it fails with a deprecation error that stops the script at `set -e`.
mc admin policy create "$ALIAS" "$POLICY" /tmp/policy.json

echo "ensuring the media service account"
# `mc admin user` rather than a service account: a service account inherits its parent's policy
# and cannot be attached to one directly, so scoping a key to this one bucket means a user of its
# own. `user add` is an upsert — it re-sets the secret of an existing user — which is what makes
# rerunning this safe after the secret changes in .env.
mc admin user add "$ALIAS" "$MEDIA_S3_ACCESS_KEY" "$MEDIA_S3_SECRET_KEY" >/dev/null

# Attaching a policy that is already attached is an error, and the only tolerable one here.
#
# This was `|| echo "already attached"` and that was worse than no handling at all: the attach
# was being OOM-killed at 64 MB, the fallback swallowed the kill, and the script printed a
# reassuring line claiming the policy was attached when it was not. The service then failed
# every upload with AccessDenied and nothing in the setup output hinted why. So the failure is
# now interrogated rather than assumed.
if attach_error=$(mc admin policy attach "$ALIAS" "$POLICY" --user "$MEDIA_S3_ACCESS_KEY" 2>&1); then
  echo "  attached $POLICY"
else
  case "$attach_error" in
    *"already"*) echo "  $POLICY was already attached" ;;
    *) echo "could not attach $POLICY: $attach_error" >&2; exit 1 ;;
  esac
fi

# Verified, not assumed. Everything above can succeed and still leave a user who cannot write,
# and finding that out here costs a second — finding it out from the media service costs a
# confused half hour reading AccessDenied.
#
# Matched with `case` rather than piped through grep, because the mc image has no grep. The first
# version of this check used one and failed on a correctly configured user, which is the most
# annoying kind of broken verification: it reports the thing it was meant to rule out.
user_info=$(mc admin user info "$ALIAS" "$MEDIA_S3_ACCESS_KEY" 2>&1 || true)
case "$user_info" in
  *"$POLICY"*) ;;
  *)
    echo "$MEDIA_S3_ACCESS_KEY exists but $POLICY is not attached to it:" >&2
    echo "$user_info" >&2
    exit 1
    ;;
esac

echo
echo "minio ready:"
echo "  bucket          $BUCKET (anonymous GetObject)"
echo "  media user      $MEDIA_S3_ACCESS_KEY  ->  $POLICY"
echo "  console         http://localhost:9001  (log in as the root user)"
