#!/usr/bin/env bash
#
# promote-to-symlink.sh — replace local freezer files with symlinks to a
# copy in another directory, for files you explicitly name via patterns.
#
# SAFETY MODEL:
#   - Dry-run by default. Nothing is touched until you pass --apply.
#   - By default a local regular file is NEVER removed; it is verified and
#     reported, and you displace it yourself.
#   - With --force, a local regular file IS removed and replaced by a symlink,
#     but ONLY if it passed verification against the source copy. A file that
#     fails verification is never deleted, regardless of --force.
#   - --force requires --apply. It is refused with --no-verify, since that
#     combination would delete data without ever checking the replacement.
#   - --backup-dir moves the local file aside instead of deleting it.
#
# USAGE:
#   ./promote-to-symlink.sh --src DIR --dst DIR [OPTIONS] PATTERN [PATTERN ...]
#
# OPTIONS:
#   --apply            Actually act (default: dry run)
#   --force            Replace VERIFIED local files with symlinks (needs --apply)
#   --backup-dir DIR   With --force: move local files here instead of deleting
#   --full-verify      Byte-for-byte compare instead of sampled head+tail
#   --chunk-mb N       Head/tail sample size in MB (default: 5)
#   --no-verify        Skip verification (incompatible with --force)
#
# EXAMPLE — see what would happen:
#   ./promote-to-symlink.sh --src /data/archive/chain \
#     --dst /var/lib/node/ancient/chain --force 'bodies.000*.cdat'
#
# EXAMPLE — do it, keeping the originals:
#   ./promote-to-symlink.sh --src /data/archive/chain \
#     --dst /var/lib/node/ancient/chain --apply --force \
#     --backup-dir /data/ancient-displaced 'bodies.000*.cdat'
#
# STOP THE NODE FIRST. Do not run --apply --force against a freezer directory
# that a running node process has open.

set -euo pipefail

SRC=""
DST=""
APPLY=0
FORCE=0
BACKUP_DIR=""
VERIFY=1
FULL_VERIFY=0
CHUNK_MB=5
PATTERNS=()

usage() {
  grep '^#' "$0" | sed -e 's/^#//' -e 's/^ //'
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src) SRC="$2"; shift 2 ;;
    --dst) DST="$2"; shift 2 ;;
    --apply) APPLY=1; shift ;;
    --force) FORCE=1; shift ;;
    --backup-dir) BACKUP_DIR="$2"; shift 2 ;;
    --full-verify) FULL_VERIFY=1; shift ;;
    --no-verify) VERIFY=0; shift ;;
    --chunk-mb) CHUNK_MB="$2"; shift 2 ;;
    -h|--help) usage ;;
    --) shift; PATTERNS+=("$@"); break ;;
    *) PATTERNS+=("$1"); shift ;;
  esac
done

if [[ -z "$SRC" || -z "$DST" || ${#PATTERNS[@]} -eq 0 ]]; then
  echo "ERROR: --src, --dst, and at least one PATTERN are required." >&2
  usage
fi

# --force is destructive; it must never run blind.
if [[ $FORCE -eq 1 && $VERIFY -eq 0 ]]; then
  echo "ERROR: --force cannot be combined with --no-verify." >&2
  echo "       That would delete local data without checking the replacement." >&2
  exit 1
fi

if [[ $FORCE -eq 1 && $APPLY -eq 0 ]]; then
  echo "NOTE: --force given without --apply — still a dry run, nothing will be deleted."
fi

[[ -d "$SRC" ]] || { echo "ERROR: source dir does not exist: $SRC" >&2; exit 1; }
[[ -d "$DST" ]] || { echo "ERROR: destination dir does not exist: $DST" >&2; exit 1; }

SRC_ABS="$(realpath -e "$SRC")"
DST_ABS="$(realpath -e "$DST")"

if [[ "$SRC_ABS" == "$DST_ABS" ]]; then
  echo "ERROR: --src and --dst resolve to the same directory. Refusing." >&2
  exit 1
fi

if [[ -n "$BACKUP_DIR" ]]; then
  if [[ $APPLY -eq 1 ]]; then
    mkdir -p "$BACKUP_DIR"
  fi
  if [[ -d "$BACKUP_DIR" ]]; then
    BACKUP_ABS="$(realpath -e "$BACKUP_DIR")"
    if [[ "$BACKUP_ABS" == "$DST_ABS" || "$BACKUP_ABS" == "$SRC_ABS" ]]; then
      echo "ERROR: --backup-dir must differ from --src and --dst." >&2
      exit 1
    fi
  else
    BACKUP_ABS="$BACKUP_DIR"
  fi
fi

CHUNK=$(( CHUNK_MB * 1024 * 1024 ))

# ---------------------------------------------------------------------------
# Hash a sample of a file: first CHUNK bytes + last CHUNK bytes.
# Files smaller than 2*CHUNK are hashed whole (avoids double-counting overlap).
# ---------------------------------------------------------------------------
sample_hash() {
  local f="$1" size="$2"
  {
    dd if="$f" bs=1M count="$CHUNK_MB" status=none
    if (( size > 2 * CHUNK )); then
      dd if="$f" bs=1M skip=$(( (size - CHUNK) / 1048576 )) status=none
    fi
  } | md5sum | cut -d' ' -f1
}

# ---------------------------------------------------------------------------
# Compare a source/destination pair. Echoes a verdict; returns 0 match / 1 differ.
# Size first: metadata only, one round trip, no bulk transfer.
# ---------------------------------------------------------------------------
verify_pair() {
  local src_f="$1" local_f="$2"
  local sa sb ha hb

  sa=$(stat -c%s "$src_f" 2>/dev/null) || { echo "cannot stat source"; return 1; }
  sb=$(stat -c%s "$local_f" 2>/dev/null) || { echo "cannot stat local"; return 1; }

  if [[ "$sa" != "$sb" ]]; then
    echo "SIZE MISMATCH source=$sa local=$sb"
    return 1
  fi

  if [[ $FULL_VERIFY -eq 1 ]]; then
    if cmp -s "$src_f" "$local_f"; then
      echo "identical (full byte compare, $sa bytes)"
      return 0
    else
      echo "CONTENT DIFFERS (full byte compare)"
      return 1
    fi
  fi

  ha=$(sample_hash "$src_f" "$sa")
  hb=$(sample_hash "$local_f" "$sb")

  if [[ "$ha" == "$hb" ]]; then
    echo "identical (size + ${CHUNK_MB}M head + ${CHUNK_MB}M tail)"
    return 0
  else
    echo "CONTENT DIFFERS (sampled head/tail: $ha vs $hb)"
    return 1
  fi
}

# findmnt -T resolves which mount covers an arbitrary path, so this works when
# --src points INSIDE a mount rather than at the mount root itself.
if ! findmnt -n -T "$SRC_ABS" >/dev/null 2>&1; then
  echo "WARNING: no mount found covering $SRC_ABS" >&2
  echo "         Symlinks would not point at a confirmed separate mount." >&2
  if [[ $FORCE -eq 1 && $APPLY -eq 1 ]]; then
    echo "ERROR: refusing --apply --force when the source is not a confirmed mount." >&2
    exit 1
  fi
else
  echo "Confirmed mounted: $(findmnt -n -o SOURCE,TARGET -T "$SRC_ABS")"
fi

echo "Source (read-only copy):    $SRC_ABS"
echo "Destination (live):         $DST_ABS"
echo "Mode: $([[ $APPLY -eq 1 ]] && echo APPLY || echo DRY-RUN)$([[ $FORCE -eq 1 ]] && echo ' + FORCE' || true)"
if [[ $VERIFY -eq 0 ]]; then
  echo "Verification: DISABLED (--no-verify)"
elif [[ $FULL_VERIFY -eq 1 ]]; then
  echo "Verification: full byte compare (slow, transfers whole files)"
else
  echo "Verification: size + ${CHUNK_MB}M head + ${CHUNK_MB}M tail"
fi
if [[ $FORCE -eq 1 ]]; then
  if [[ -n "$BACKUP_DIR" ]]; then
    echo "Force mode: VERIFIED local files moved to $BACKUP_DIR, then symlinked"
  else
    echo "Force mode: VERIFIED local files DELETED, then symlinked (no backup)"
  fi
fi
echo

count_link=0
count_replaced=0
count_skip_exists_ok=0
count_skip_exists_bad=0
count_skip_linked=0
count_skip_wrong=0
count_missing=0
count_verify_fail=0

for pattern in "${PATTERNS[@]}"; do
  matched_any=0

  while IFS= read -r -d '' srcfile; do
    matched_any=1
    fname="$(basename "$srcfile")"
    dstfile="$DST_ABS/$fname"
    target="$SRC_ABS/$fname"

    # --- already a symlink -------------------------------------------------
    if [[ -L "$dstfile" ]]; then
      current_target="$(readlink -f "$dstfile" || true)"
      if [[ "$current_target" == "$(readlink -f "$target")" ]]; then
        echo "OK       already linked: $fname"
        count_skip_linked=$((count_skip_linked + 1))
      else
        echo "SKIP     $fname is a symlink pointing elsewhere ($current_target) — not touching"
        count_skip_wrong=$((count_skip_wrong + 1))
      fi
      continue
    fi

    # --- local regular file present ---------------------------------------
    if [[ -e "$dstfile" ]]; then

      if [[ $VERIFY -eq 0 ]]; then
        # Only reachable without --force (the combination is refused above).
        echo "SKIP     $fname exists locally as a regular file (unverified)"
        count_skip_exists_ok=$((count_skip_exists_ok + 1))
        continue
      fi

      if ! verdict=$(verify_pair "$target" "$dstfile"); then
        echo "DANGER   $fname exists locally — VERIFY FAILED: $verdict"
        echo "         -> NOT replaced, even with --force. Investigate."
        count_skip_exists_bad=$((count_skip_exists_bad + 1))
        count_verify_fail=$((count_verify_fail + 1))
        continue
      fi

      # Verified identical.
      if [[ $FORCE -eq 0 ]]; then
        echo "SKIP     $fname exists locally — VERIFIED $verdict"
        echo "         -> re-run with --force to replace it with a symlink"
        count_skip_exists_ok=$((count_skip_exists_ok + 1))
        continue
      fi

      if [[ $APPLY -eq 0 ]]; then
        if [[ -n "$BACKUP_DIR" ]]; then
          echo "WOULD REPLACE  $fname (VERIFIED $verdict) — move to $BACKUP_DIR, then symlink"
        else
          echo "WOULD REPLACE  $fname (VERIFIED $verdict) — DELETE local, then symlink"
        fi
        count_replaced=$((count_replaced + 1))
        continue
      fi

      # Apply + force + verified: displace, then link.
      if [[ -n "$BACKUP_DIR" ]]; then
        if [[ -e "$BACKUP_ABS/$fname" ]]; then
          echo "SKIP     $fname — backup already exists at $BACKUP_ABS/$fname, refusing to clobber"
          count_skip_exists_bad=$((count_skip_exists_bad + 1))
          continue
        fi
        mv -- "$dstfile" "$BACKUP_ABS/$fname"
        ln -s -- "$target" "$dstfile"
        echo "REPLACED $fname -> $target (original moved to $BACKUP_ABS/$fname)"
      else
        rm -f -- "$dstfile"
        ln -s -- "$target" "$dstfile"
        echo "REPLACED $fname -> $target (original deleted)"
      fi
      count_replaced=$((count_replaced + 1))
      continue
    fi

    # --- no local copy: nothing to verify against, safe to link ------------
    if [[ $APPLY -eq 1 ]]; then
      ln -s -- "$target" "$dstfile"
      echo "LINKED   $fname -> $target"
    else
      echo "WOULD LINK  $fname -> $target (no local copy to verify against)"
    fi
    count_link=$((count_link + 1))

  done < <(find "$SRC_ABS" -maxdepth 1 -type f -name "$pattern" -print0 | sort -z)

  if [[ $matched_any -eq 0 ]]; then
    echo "WARNING  pattern matched nothing in source: $pattern"
    count_missing=$((count_missing + 1))
  fi
done

echo
echo "Summary:"
echo "  linked / would-link (no local copy): $count_link"
echo "  replaced / would-replace (verified): $count_replaced"
echo "  already correctly linked:            $count_skip_linked"
echo "  local file present, left in place:   $count_skip_exists_ok"
echo "  local file present, VERIFY FAILED:   $count_skip_exists_bad"
echo "  symlink pointing elsewhere:          $count_skip_wrong"
echo "  patterns matching nothing:           $count_missing"

if [[ $count_verify_fail -gt 0 ]]; then
  echo
  echo "!! $count_verify_fail file(s) FAILED verification and were left untouched."
fi

if [[ $APPLY -eq 0 ]]; then
  echo
  if [[ $FORCE -eq 1 ]]; then
    echo "(dry run — re-run with --apply --force to actually replace)"
  else
    echo "(dry run — re-run with --apply to actually create links)"
  fi
fi

[[ $count_verify_fail -eq 0 ]]