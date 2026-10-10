"""Access checks recover temporary failures but never bypass the whitelist."""
import unittest
from unittest.mock import MagicMock, patch

import requests
from lecture_trimmer.runtime.messages import public_message
from lecture_trimmer.auth.access_control import AccessUnavailable
from lecture_trimmer.integrations.pw_proxy import PWSession, load_kit


class AccessRecoveryTests(unittest.TestCase):
    def kit(self, statuses):
        kit=MagicMock()
        kit._session={}
        kit._access_diagnostic={}
        kit.check_allowed_status.side_effect=statuses
        kit.new_task_id.return_value='test-job'
        return kit

    def test_job_preflight_reuses_just_completed_authorization(self):
        kit=self.kit(['allowed'])
        session=PWSession('private-token',expires_at=500,clock=lambda:100,kit=kit)
        job=session.job()
        job.ensure_allowed()
        kit.check_allowed_status.assert_called_once()
        session.close()
        with self.assertRaises(PermissionError): job.ensure_allowed()

    def test_transient_failure_recovers_but_persistent_outage_fails_closed(self):
        for statuses, allowed in [(['error','allowed'],True), (['error']*3,False)]:
            kit=self.kit(statuses)
            session=PWSession('private-token',expires_at=500,clock=lambda:100,kit=kit)
            with patch('lecture_trimmer.integrations.pw_proxy.interruptible_wait') as wait:
                if allowed: session.ensure_allowed()
                else:
                    with self.assertRaises(AccessUnavailable): session.ensure_allowed()
            self.assertEqual(kit.check_allowed_status.call_count,len(statuses))
            self.assertEqual(wait.call_count,len(statuses)-1)
            kit.gemini_generate.assert_not_called()

    def test_denied_account_is_never_retried_or_accepted(self):
        kit=self.kit(['denied'])
        session=PWSession('private-token',expires_at=500,clock=lambda:100,kit=kit)
        with patch('lecture_trimmer.integrations.pw_proxy.interruptible_wait') as wait:
            with self.assertRaises(PermissionError): session.ensure_allowed()
        wait.assert_not_called()
        self.assertFalse(session._active)

    def test_expired_remote_session_requires_login(self):
        kit=self.kit(['error'])
        kit._access_diagnostic={'http_status':401,'category':'access_expired'}
        session=PWSession('private-token',expires_at=500,clock=lambda:100,kit=kit)
        with self.assertRaises(PermissionError): session.ensure_allowed()
        self.assertFalse(session._active)

    def test_revocation_is_checked_after_cache_expires(self):
        now=[100]
        kit=self.kit(['allowed','denied'])
        session=PWSession('private-token',expires_at=500,clock=lambda:now[0],kit=kit)
        job=session.job()
        now[0]=161
        with self.assertRaises(PermissionError): job.ensure_allowed()

    def test_transport_failure_records_only_safe_diagnostics(self):
        kit=load_kit()
        kit._session.update(token='private-token',expiry=9999999999)
        with patch.object(kit.requests,'post',side_effect=requests.Timeout('SECRET response/token')):
            self.assertEqual(kit.check_allowed_status('google-token'),'error')
        self.assertEqual(kit._access_diagnostic,{'category':'access_timeout','http_status':None})
        self.assertNotIn('SECRET',str(kit._access_diagnostic))

    def test_outage_message_is_actionable_without_private_details(self):
        message=public_message(AccessUnavailable('SECRET'))
        self.assertIn('PW access service',message)
        self.assertNotIn('SECRET',message)


if __name__=='__main__': unittest.main()
