import numpy as np
from scipy.stats import pearsonr
from sklearn.linear_model import Ridge


def train_test_reg(X_train, X_test, y_train, y_test, 
                   max_iter=1000, random_state=0, genes=None, alpha=None):
    
    alpha = 100 / (X_train.shape[1] * y_train.shape[1])

    print(f"Using alpha: {alpha}")
    reg = Ridge(solver='lsqr',
                alpha=alpha, 
                random_state=random_state, 
                fit_intercept=False, 
                max_iter=max_iter)
    reg.fit(X_train, y_train)
    preds_all = reg.predict(X_test)
    
    errors = []
    r2_scores = []
    pearson_corrs = []
    pearson_genes = []
    i = 0
    for target in range(y_test.shape[1]):
        preds = preds_all[:, target]
        target_vals = y_test[:, target]
        l2_error = float(np.mean((preds - target_vals)**2))
        # compute r2 score
        r2_score = float(1 - np.sum((target_vals - preds)**2) / np.sum((target_vals - np.mean(target_vals))**2))
        pearson_corr, _ = pearsonr(target_vals, preds)
        if np.isnan(pearson_corr):
            print(target_vals)
            print(preds)
        errors.append(l2_error)
        r2_scores.append(r2_score)
        pearson_corrs.append(pearson_corr)
        score_dict = {
            'name': genes[i],
            'pearson_corr': pearson_corr,
        }
        pearson_genes.append(score_dict)
        i += 1
        

    results = {'l2_errors': list(errors), 
               'r2_scores': list(r2_scores),
               'pearson_corrs': pearson_genes,
               'pearson_mean': float(np.mean(pearson_corrs)),
               'pearson_std': float(np.std(pearson_corrs)),
               'l2_error_q1': float(np.percentile(errors, 25)),
               'l2_error_q2': float(np.median(errors)),
               'l2_error_q3': float(np.percentile(errors, 75)),
               'r2_score_q1': float(np.percentile(r2_scores, 25)),
               'r2_score_q2': float(np.median(r2_scores)),
               'r2_score_q3': float(np.percentile(r2_scores, 75)),}
    dump = {
        'preds_all': preds_all,
        'targets_all': y_test,
    }
    
    return results, dump