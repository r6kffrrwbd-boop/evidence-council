## Council verdict: multiple_supported

Several different answers passed every objective check.

**Answer:** def is_leap(year: int) -> bool:
    """Return whether year is a leap year in the Gregorian calendar."""
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


**Participants (adapters that actually ran):** codex, ollama:qwen3.5:9b, ollama:qwen3.5:4b
**Excluded:** claude: not in providers.requested; gemini: not in providers.requested; hermes: not in providers.requested; grok: not in providers.requested

**Checks actually run:** unit-tests (python_tests)

**Why:**
- Option A passed selecting check(s): unit-tests (python_tests): exit 0 (sandboxed); 5 tests passed
- Other options also passed every selecting check: B, C. The ranking among them (fewer contradicted claims, then more passing checks) is not decisive evidence.

**Still unverified:**
- claim p1.c1: A Gregorian year is a leap year exactly when divisible by 4 and either not divisible by 100 or divisible by 400.

**Uncertainty:** medium — selection rests on a single selecting check; several different answers passed the checks
