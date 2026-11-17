# Merge-outcome bot demo

A throwaway file for trying review-app's "after the merge" bot, the Merge outcome workflow in
`.github/workflows/merge-outcome.yml`.

This pull request targets the throwaway branch `review-app-demo/base`, not `main`. Once it's
merged, github-actions[bot] should comment on it with "review-app: after the merge". It should
then update that same comment when CI on the merge commit finishes.

Nothing here is meant for `main`. Both `review-app-demo/*` branches can be deleted after the demo.
