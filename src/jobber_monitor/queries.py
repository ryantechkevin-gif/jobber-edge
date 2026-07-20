"""
GraphQL query strings.

Field names for `account`, `clients`, `invoices`, and `quotes` below are
confirmed against real Jobber integration examples (Maton's Jobber
connector reference, cross-checked against Jobber's own API docs).

Jobs, requests, visits, expenses, and the "client communications" dataset
referenced in WeSpeakWiFi's existing Client Communications Audit report
are NOT yet confirmed against a live schema -- rather than guess field
names and risk a query that's subtly wrong (or silently missing fields),
use INTROSPECT_TYPE_QUERY via the /api/jobber/schema route once OAuth is
connected to check the real shape before adding a query for any of those.
"""

ACCOUNT_QUERY = """
query {
  account {
    id
    name
  }
}
"""

CLIENTS_QUERY = """
query ClientsPage($first: Int!, $after: String) {
  clients(first: $first, after: $after) {
    nodes {
      id
      name
      isCompany
      isArchived
      createdAt
      emails { address }
      phones { number }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

INVOICES_QUERY = """
query InvoicesPage($first: Int!, $after: String) {
  invoices(first: $first, after: $after) {
    nodes {
      id
      invoiceNumber
      invoiceStatus
      total
      createdAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

QUOTES_QUERY = """
query QuotesPage($first: Int!, $after: String) {
  quotes(first: $first, after: $after) {
    nodes {
      id
      quoteNumber
      title
      quoteStatus
      createdAt
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""

# Ad-hoc schema exploration -- see the /api/jobber/schema route.
INTROSPECT_TYPE_QUERY = """
query IntrospectType($name: String!) {
  __type(name: $name) {
    name
    fields {
      name
      type { name kind ofType { name kind } }
    }
  }
}
"""
