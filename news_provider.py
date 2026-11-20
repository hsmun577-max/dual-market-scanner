"""Prioritized normalized provider results, inspired by DSA's provider strategy layer."""

def collect_news(ticker,now,parse_yahoo,local_loader=None):
    attempts=[];checked_empty=False
    if local_loader is not None:
        try:
            news=local_loader()
            attempts.append(dict(provider='domestic_direct',status='checked',count=len(news)))
            if news:return dict(news=news,news_status='ok',news_provider='domestic_direct',news_provider_attempts=attempts)
            checked_empty=True
        except Exception as exc:attempts.append(dict(provider='domestic_direct',status='unavailable',error=type(exc).__name__))
    try:
        news=parse_yahoo(ticker.get_news(count=12),now)
        attempts.append(dict(provider='yahoo',status='checked',count=len(news)))
        return dict(news=news,news_status='ok' if news else 'no_recent_articles',news_provider='yahoo',news_provider_attempts=attempts)
    except Exception as exc:
        attempts.append(dict(provider='yahoo',status='unavailable',error=type(exc).__name__))
        return dict(news=[],news_status='no_recent_articles' if checked_empty else 'unavailable',
            news_provider='domestic_direct' if checked_empty else None,news_provider_attempts=attempts)
