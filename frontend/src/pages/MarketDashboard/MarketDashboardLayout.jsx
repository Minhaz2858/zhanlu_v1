import MarketDashboardPage from './MarketDashboardPage';
import DashboardChatWidget from './components/DashboardChatWidget';

/**
 * Layout wrapper for the EDIA Market Dashboard route.
 *
 * Renders the page content + the floating chatbot widget.
 * Using a wrapper avoids touching MarketDashboardPage's 5 early returns
 * — the widget always renders alongside whatever the page produces.
 */
export default function MarketDashboardLayout() {
  return (
    <>
      <MarketDashboardPage />
      <DashboardChatWidget />
    </>
  );
}
