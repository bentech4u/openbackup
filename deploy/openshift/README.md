# OpenShift service accounts

OpenBackup can create these itself: **Clusters → Add cluster → Set up with
admin credentials** (kubeadmin or any cluster-admin, used once and never
stored).

To create them by hand instead, apply the manifests from the package and
paste the tokens into **Add cluster → Use existing tokens**:

```bash
oc apply -f /opt/openbackup/openbackup/kube/manifests/backup-serviceaccount.yaml
oc -n openbackup get secret openbackup-backup-token -o jsonpath='{.data.token}' | base64 -d

# Only on clusters you restore into:
oc apply -f /opt/openbackup/openbackup/kube/manifests/restore-serviceaccount.yaml
oc -n openbackup get secret openbackup-restore-token -o jsonpath='{.data.token}' | base64 -d
```
