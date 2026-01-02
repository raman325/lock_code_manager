import { LockCodeManagerDashboardStrategy } from './dashboard-strategy';
import './lock-code-data-card';
import './lock-code-data-card-editor';
import { LockCodeManagerViewStrategy } from './view-strategy';

declare global {
    interface HTMLElementTagNameMap {
        'll-strategy-dashboard-lock-code-manager': LockCodeManagerDashboardStrategy;
        'll-strategy-view-lock-code-manager': LockCodeManagerViewStrategy;
    }
}

customElements.define('ll-strategy-dashboard-lock-code-manager', LockCodeManagerDashboardStrategy);
customElements.define('ll-strategy-view-lock-code-manager', LockCodeManagerViewStrategy);
