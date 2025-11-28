# Dango MVP Plan

**Last Updated:** November 26, 2025
**Target:** v0.1.0 Release

---

## MVP Scope

### Must Have (Blocking for Release)

#### Data Sources - OAuth Providers
| Provider | OAuth Status | Test Status | Notes |
|----------|-------------|-------------|-------|
| Google Sheets | ✅ Working | ✅ PASSED | Full end-to-end tested |
| Shopify | ✅ Code Fixed | ⏳ Pending | Ready for testing |
| Google Ads | ✅ Code Fixed | ⏳ Pending | Needs developer token |
| Google Analytics | ✅ Code Fixed | ⏳ Pending | Needs GA4 property |
| Facebook Ads | ✅ Code Fixed | ⏳ Pending | Needs FB developer account |

#### Data Sources - API Key/Basic Auth
| Provider | Status | Notes |
|----------|--------|-------|
| CSV | ✅ Working | No auth needed |
| Stripe | ✅ Working | API key auth |
| HubSpot | ✅ Registry | API key auth |

#### Core Features
| Feature | Status | Notes |
|---------|--------|-------|
| `dango init` | ✅ Working | Project initialization |
| `dango source add` | ✅ Working | Source wizard |
| `dango auth <provider>` | ✅ Working | OAuth/API key setup |
| `dango sync` | ✅ Working | Data loading via dlt |
| `dango model add` | ✅ Working | dbt model wizard |
| `dango run` | ✅ Working | dbt execution + Metabase sync |
| `dango docs` | ✅ Working | Open dbt docs |

#### Visualization
| Feature | Status | Notes |
|---------|--------|-------|
| Metabase Docker | ✅ Working | Auto-starts on init |
| Schema sync | ✅ Working | After dbt run |
| Raw schemas hidden | ✅ Working | Only staging/intermediate/marts visible |

---

## Testing Status

### Completed
- [x] Google Sheets OAuth end-to-end
- [x] Multi-sheet selection
- [x] Staging model auto-generation
- [x] Intermediate/Marts model creation
- [x] Metabase schema visibility

### In Progress
- [ ] Shopify OAuth testing
- [ ] Google Ads/Analytics OAuth testing
- [ ] Facebook Ads OAuth testing
- [ ] Edge cases (missing credentials, multiple sources)

### Not Started
- [ ] Advanced user validation (dlt_native, custom macros, custom schemas)
- [ ] Windows compatibility testing
- [ ] Port conflict handling

---

## Remaining Work for MVP

### Priority 1: Complete OAuth Testing (~2-3 hours)

1. **Shopify OAuth** (30 min)
   - Create Shopify trial account
   - Run `dango auth shopify`
   - Add source, sync, verify data

2. **Google Ads/Analytics** (45 min)
   - Enable APIs in existing Google Cloud project
   - Run `dango auth google --service analytics`
   - Verify OAuth flow works (may not have actual data)

3. **Facebook Ads** (30 min)
   - Set up Facebook developer account
   - Run `dango auth facebook_ads`
   - Verify token exchange works

4. **Edge Cases** (30 min)
   - Test missing credentials error message
   - Test multiple sources with same OAuth
   - Verify error messages are helpful

5. **Advanced User Validation** (30 min) - NEW
   - Test `dlt_native` bypass mode (manual source config)
   - Test custom dbt macros
   - Test custom schemas beyond staging/intermediate/marts
   - Test dbt packages installation
   - Verify direct dbt CLI access works

### Priority 2: Documentation Review

- [ ] Update README.md with OAuth instructions
- [ ] Review OAUTH_SETUP.md for accuracy
- [ ] Add troubleshooting section for common errors

### Priority 3: Pre-Release Checklist

- [ ] Bump version number
- [ ] Update CHANGELOG.md
- [ ] Create release notes
- [ ] Tag release in git

---

## Known Limitations for MVP

1. **OAuth Providers Requiring Complex Setup**
   - Google Ads: Needs developer token approval (can take days)
   - Facebook Ads: Token expires in 60 days, needs renewal

2. **Platform Support**
   - Windows: No timeout on OAuth flow (signal.alarm not available)
   - Tested primarily on macOS

3. **dlt Source Limitations**
   - Some sources have bugs (we fixed Shopify, Google Sheets)
   - `dlt_native` mode is escape hatch for advanced users

---

## Post-MVP Roadmap

### v0.2.0
- [ ] Token refresh/expiration warnings
- [ ] More sources tested and supported
- [ ] Improved error messages
- [ ] Windows full support

### v0.3.0
- [ ] Multi-tenant OAuth (users don't need their own Google Cloud project)
- [ ] Cloud deployment option
- [ ] Scheduled sync support

---

## Success Criteria for MVP

### Must Pass
- [ ] All OAuth providers complete authentication flow
- [ ] Data syncs without errors for at least one source per provider
- [ ] Metabase shows correct schemas
- [ ] Error messages are helpful (not cryptic dlt errors)

### Should Pass
- [ ] Multiple sources same OAuth work
- [ ] Edge cases handled gracefully

### Nice to Have
- [ ] All API key sources tested
- [ ] Windows tested
