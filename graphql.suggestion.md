LessWrong AI tag: skip RSS, use GraphQL. Even fixed, GW's RSS is scraping the same shaky infrastructure. The robust path for a programmatic tool: LessWrong and AF both run ForumMagnum, which exposes a public GraphQL endpoint at https://www.lesswrong.com/graphql (no auth for reads; it's what most LW bots use). You can query posts filtered by tag, sorted by new, with baseScore returned so you can apply your own karma threshold instead of hoping a URL param works. Schematically:
```graphql{
  posts(input: {terms: {
    filterSettings: {tags: [{tagId: "<AI-tag-id>", filterMode: "Required"}]},
    sortedBy: "new", limit: 50
  }}) {
    results { title pageUrl postedAt baseScore user { displayName } }
  }
}```
I don't trust my memory of the AI tag's ID, so pull it via a tag(input:...) query or from the tag page source rather than taking a guess from me. One design note: LW's AI tag at karma ≥30–40 is a genuinely different distribution from AF — more takes, less research — so give it its own low source prior rather than sharing AF's.
