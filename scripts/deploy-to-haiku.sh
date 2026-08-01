#!/bin/sh
# Deploy the current branch to a Haiku machine — without being able to
# destroy work made on that machine.
#
# The old flow was `tar | ssh tar x`, and it had exactly the failure a
# deploy flow must not have: an agent session running ON the Haiku box had
# fixed a real bug in its working copy, and the next deploy overwrote the
# fix without anyone noticing. Git refuses where tar obeys: a dirty working
# copy stops the deploy, and work committed on the box is one fetch away
# from coming home instead of being paved over.
#
# One-time setup on the Haiku box:
#     git init --bare /boot/home/haikode.git
#     cd /boot/home/haikode
#     git init -b main
#     git remote add origin /boot/home/haikode.git
#     # after the first push from here:
#     git fetch origin && git reset origin/main && git checkout -f -- .
#
# Then, from this repository:
#     HAIKODE_DEPLOY_HOST=user@<address> scripts/deploy-to-haiku.sh
#
# The address stays in your environment (or in `git remote add haiku …`),
# never in this file: this repository is public and your machine is not.
#
# To bring commits made on the box back home:
#     git fetch "ssh://$HAIKODE_DEPLOY_HOST$HAIKODE_DEPLOY_PATH" main
#     git log --oneline FETCH_HEAD --not main    # what the box has that we lack
set -eu

HOST="${HAIKODE_DEPLOY_HOST:?set HAIKODE_DEPLOY_HOST=user@<haiku-machine>}"
TREE="${HAIKODE_DEPLOY_PATH:-/boot/home/haikode}"
BARE="${HAIKODE_DEPLOY_BARE:-$TREE.git}"
BRANCH="${HAIKODE_DEPLOY_BRANCH:-main}"

echo "==> pushing $BRANCH to $BARE"
git push "ssh://$HOST$BARE" "HEAD:refs/heads/$BRANCH"

echo "==> updating the working copy in $TREE"
# -uno: untracked files (build artifacts, scratch dirs) do not block a
# deploy; edits to tracked files do — those are somebody's work.
ssh "$HOST" "
	set -eu
	cd '$TREE'
	dirty=\$(git status --porcelain -uno)
	if [ -n \"\$dirty\" ]; then
		echo 'deploy refused: the working copy on this machine has local edits:' >&2
		echo \"\$dirty\" >&2
		echo 'Commit them there (git add -A && git commit), fetch them home,' >&2
		echo 'and deploy again. Overwriting them is how a fix got lost once.' >&2
		exit 1
	fi
	git fetch -q origin
	git merge --ff-only \"origin/$BRANCH\"
"

echo "==> deployed $(git rev-parse --short HEAD)"
