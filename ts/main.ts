import { LockCodeManagerDashboardStrategy } from './dashboard-strategy';
import './lock-codes-card';
import './lock-codes-card-editor';
import './slot-card';
import './slot-card-editor';
import { LockCodeManagerViewStrategy } from './view-strategy';

declare global {
    interface HTMLElementTagNameMap {
        'll-strategy-dashboard-lock-code-manager': LockCodeManagerDashboardStrategy;
        'll-strategy-view-lock-code-manager': LockCodeManagerViewStrategy;
    }
}

customElements.define('ll-strategy-dashboard-lock-code-manager', LockCodeManagerDashboardStrategy);
customElements.define('ll-strategy-view-lock-code-manager', LockCodeManagerViewStrategy);
