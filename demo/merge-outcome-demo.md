# Merge-outcome bot demo

A throwaway file for trying review-app's "after the merge" bot, the Merge outcome workflow in
`.github/workflows/merge-outcome.yml`.

This pull request targets the throwaway branch `review-app-demo/base`, not `main`. Once it's
merged, github-actions[bot] should comment on it with "review-app: after the merge". It should
then update that same comment when CI on the merge commit finishes.

Nothing here is meant for `main`. Both `review-app-demo/*` branches can be deleted after the demo.

Second try: the first demo PR, #21, got no bot run. GitHub runs `pull_request_target` from
`main`'s copy of a workflow, and `main` doesn't have the bot yet. The bot now starts from
`pull_request`, which runs the merge commit's copy.
