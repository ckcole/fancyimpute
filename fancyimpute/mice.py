# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function, division

from time import time

from six.moves import range
import numpy as np
import pickle

from .bayesian_ridge_regression import BayesianRidgeRegression
from .solver import Solver


class MICE(Solver):
    """
    Basic implementation of MICE package from R.
    This version assumes all of the columns are ordinal,
    and uses ridge regression.

        Parameters
        ----------
        visit_sequence : str
            Possible values: "monotone" (default), "roman", "arabic",
                "revmonotone".

        n_imputations : int
            Defaults to 100

        n_burn_in : int
            Defaults to 10

        impute_type : str
            "pmm" is probablistic moment matching.
            "col" (default) means fill in with samples from posterior predictive
                distribution.

        n_pmm_neighbors : int
            Number of nearest neighbors for PMM, defaults to 5.

        model : predictor function
            A model that has fit, predict, and predict_dist methods.
            Defaults to BayesianRidgeRegression(lambda_reg=0.001).
            Note that the regularization parameter lambda_reg
            is by default scaled by np.linalg.norm(np.dot(X.T,X)).
            Sensible lambda_regs to try: 0.25, 0.1, 0.01, 0.001, 0.0001.

        n_nearest_columns : int
            Number of other columns to use to estimate current column.
            Useful when number of columns is huge.
            Default is to use all columns.

        init_fill_method : str
            Valid values: {"mean", "median", or "random"}
            (the latter meaning fill with random samples from the observed
            values of a column)

        min_value : float
            Minimum possible imputed value

        max_value : float
            Maximum possible imputed value

        fitted_models : array
            Holds fitted models for each column through iterations.
            Rows correspond to iterations, columns correspond to the columns
            in the array to complete.

        visit_indices : array
            Order in which columns were visited during imputation.

        column_init_values : array
            Initial values used to fill missing values by column,
            depending on choice of init_fill_method.

        seed : int
            Used to set the random seed in order for results to be
            reproducible.

        verbose : boolean
    """

    def __init__(
            self,
            visit_sequence='monotone',  # order in which we visit the columns
            n_imputations=100,
            n_burn_in=10,  # this many replicates will be thrown away
            n_pmm_neighbors=5,  # number of nearest neighbors in PMM
            impute_type='col',  # also can be pmm
            model=BayesianRidgeRegression(lambda_reg=0.001, add_ones=True),
            n_nearest_columns=np.infty,
            init_fill_method="mean",
            min_value=None,
            max_value=None,
            seed=None,
            verbose=True):
        """
        Parameters
        ----------
        visit_sequence : str
            Possible values: "monotone" (default), "roman", "arabic",
                "revmonotone".

        n_imputations : int
            Defaults to 100

        n_burn_in : int
            Defaults to 10

        impute_type : str
            "ppm" is probablistic moment matching.
            "col" (default) means fill in with samples from posterior predictive
                distribution.

        n_pmm_neighbors : int
            Number of nearest neighbors for PMM, defaults to 5.

        model : predictor function
            A model that has fit, predict, and predict_dist methods.
            Defaults to BayesianRidgeRegression(lambda_reg=0.001).
            Note that the regularization parameter lambda_reg
            is by default scaled by np.linalg.norm(np.dot(X.T,X)).
            Sensible lambda_regs to try: 0.1, 0.01, 0.001, 0.0001.

        n_nearest_columns : int
            Number of other columns to use to estimate current column.
            Useful when number of columns is huge.
            Default is to use all columns.

        init_fill_method : str
            Valid values: {"mean", "median", or "random"}
            (the latter meaning fill with random samples from the observed
            values of a column)

        fitted_models : array
            Holds fitted models for each column through iterations.
            Rows correspond to iterations, columns correspond to the columns
            in the array to complete.

        visit_indices : array
            Order in which columns were visited during imputation.

        column_init_values : array
            Initial values used to fill missing values by column,
            depending on choice of init_fill_method.

        seed : int
            Used to set the random seed in order for results to be
            reproducible.

        verbose : boolean
        """
        Solver.__init__(
            self,
            n_imputations=n_imputations,
            min_value=min_value,
            max_value=max_value,
            fill_method=init_fill_method)
        self.visit_sequence = visit_sequence
        self.n_burn_in = n_burn_in
        self.n_pmm_neighbors = n_pmm_neighbors
        self.impute_type = impute_type
        self.model = model
        self.n_nearest_columns = n_nearest_columns
        self.verbose = verbose
        self.fitted_models = None
        self.visit_indices = None
        self.column_init_values = None
        self.seed = seed
        if self.seed is not None:
            np.random.seed(self.seed)

    def perform_imputation_round(
            self,
            X_filled,
            missing_mask,
            observed_mask,
            visit_indices,
            round_index=None):
        """
        Does one entire round-robin set of updates.

        If the index of the round is specified, save the fitted models.
        TODO reset the seed here to ensure we'll always fit the same model?
        """
        n_rows, n_cols = X_filled.shape

        if n_cols > self.n_nearest_columns:
            # make a correlation matrix between all the original columns,
            # excluding the constant ones
            correlation_matrix = np.corrcoef(X_filled, rowvar=0)
            abs_correlation_matrix = np.abs(correlation_matrix)

        n_missing_for_each_column = missing_mask.sum(axis=0)
        ordered_column_indices = np.arange(n_cols)

        for col_idx in visit_indices:
            # which rows are missing for this column
            missing_row_mask_for_this_col = missing_mask[:, col_idx]
            n_missing_for_this_col = n_missing_for_each_column[col_idx]
            if n_missing_for_this_col > 0:  # if we have any missing data at all
                observed_row_mask_for_this_col = observed_mask[:, col_idx]
                column_values = X_filled[:, col_idx]
                column_values_observed = column_values[observed_row_mask_for_this_col]

                if n_cols <= self.n_nearest_columns:
                    other_column_indices = np.concatenate([
                        ordered_column_indices[:col_idx],
                        ordered_column_indices[col_idx + 1:]
                    ])
                else:
                    # probability of column draw is proportional to absolute
                    # pearson correlation
                    p = abs_correlation_matrix[col_idx, :].copy()

                    # adding a small amount of weight to every bin to make sure
                    # every column has some small chance of being chosen
                    p += 0.0000001

                    # make the probability of choosing the current column
                    # zero
                    p[col_idx] = 0

                    p /= p.sum()
                    other_column_indices = np.random.choice(
                        ordered_column_indices,
                        self.n_nearest_columns,
                        replace=False,
                        p=p)
                X_other_cols = X_filled[:, other_column_indices]
                X_other_cols_observed = X_other_cols[observed_row_mask_for_this_col]
                brr = self.model
                brr.fit(
                    X_other_cols_observed,
                    column_values_observed,
                    inverse_covariance=None)
                # Save fitted model for this column
                if round_index is not None:
                    self.fitted_models[round_index][col_idx] = brr

                # Now we choose the row method (PMM) or the column method.
                if self.impute_type == 'pmm':  # this is the PMM procedure
                    # predict values for missing values using random beta draw
                    X_missing = X_filled[
                        np.ix_(missing_row_mask_for_this_col, other_column_indices)]
                    col_preds_missing = brr.predict(X_missing, random_draw=True)
                    # predict values for observed values using best estimated beta
                    X_observed = X_filled[
                        np.ix_(observed_row_mask_for_this_col, other_column_indices)]
                    col_preds_observed = brr.predict(X_observed, random_draw=False)
                    # for each missing value, find its nearest neighbors in the observed values
                    D = np.abs(col_preds_missing[:, np.newaxis] - col_preds_observed)  # distances
                    # take top k neighbors
                    k = np.minimum(self.n_pmm_neighbors, len(col_preds_observed) - 1)
                    k_nearest_indices = np.argpartition(D, k, 1)[:, :k]  # <- bottleneck!
                    # pick one of the nearest neighbors at random! that's right!
                    imputed_indices = np.array([
                        np.random.choice(neighbor_index)
                        for neighbor_index in k_nearest_indices])
                    # set the missing values to be the values of the nearest
                    # neighbor in the output space
                    imputed_values = column_values_observed[imputed_indices]
                elif self.impute_type == 'col':
                    X_other_cols_missing = X_other_cols[missing_row_mask_for_this_col]
                    # predict values for missing values using posterior predictive draws
                    # see the end of this:
                    # https://www.cs.utah.edu/~fletcher/cs6957/lectures/BayesianLinearRegression.pdf
                    mus, sigmas_squared = brr.predict_dist(X_other_cols_missing)
                    # inplace sqrt of sigma_squared
                    sigmas = sigmas_squared
                    np.sqrt(sigmas_squared, out=sigmas)
                    imputed_values = np.random.normal(mus, sigmas)
                imputed_values = self.clip(imputed_values)
                X_filled[missing_row_mask_for_this_col, col_idx] = imputed_values
        return X_filled

    def initialize(self, X, missing_mask, observed_mask, visit_indices):
        """
        Initialize the missing values by simple sampling from the same column.
        """
        # lay out X's elements in Fortran/column-major order since it's
        # often going to be accessed one column at a time
        X_filled = X.copy(order="F")
        self.column_init_values = np.zeros(X.shape[1])
        for col_idx in visit_indices:
            missing_mask_col = missing_mask[:, col_idx]
            n_missing = missing_mask_col.sum()
            if n_missing > 0:
                observed_row_mask_for_col = observed_mask[:, col_idx]
                column = X_filled[:, col_idx]
                observed_column = column[observed_row_mask_for_col]

                if self.fill_method == "mean":
                    fill_values = np.mean(observed_column)
                elif self.fill_method == "median":
                    fill_values = np.median(observed_column)
                elif self.fill_method == "random":
                    fill_values = np.random.choice(observed_column, n_missing)
                else:
                    raise ValueError("Invalid fill method %s" % self.fill_method)
                self.column_init_values[col_idx] = fill_values
                X_filled[missing_mask_col, col_idx] = fill_values
        return X_filled

    def get_visit_indices(self, missing_mask):
        """
        Decide what order we will update the columns.
        As a homage to the MICE package, we will have 4 options of
        how to order the updates.
        """
        n_rows, n_cols = missing_mask.shape
        if self.visit_sequence == 'roman':
            return np.arange(n_cols)
        elif self.visit_sequence == 'arabic':
            return np.arange(n_cols - 1, -1, -1)  # same as np.arange(d)[::-1]
        elif self.visit_sequence == 'monotone':
            return np.argsort(missing_mask.sum(0))[::-1]
        elif self.visit_sequence == 'revmonotone':
            return np.argsort(missing_mask.sum(0))
        else:
            raise ValueError("Invalid choice for visit order: %s" % self.visit_sequence)

    def multiple_imputations(self, X):
        """
        Expects 2d float matrix with NaN entries signifying missing values

        Returns a sequence of arrays of the imputed missing values
        of length self.n_imputations, and a mask that specifies where these values
        belong in X.
        """
        start_t = time()
        X = np.asarray(X)
        self._check_input(X)
        missing_mask = np.isnan(X)
        self._check_missing_value_mask(missing_mask)

        visit_indices = self.get_visit_indices(missing_mask)
        self.visit_indices = visit_indices
        # since we're accessing the missing mask one column at a time,
        # lay it out so that columns are contiguous
        missing_mask = np.asarray(missing_mask, order="F")
        observed_mask = ~missing_mask

        X_filled = self.initialize(
            X,
            missing_mask=missing_mask,
            observed_mask=observed_mask,
            visit_indices=visit_indices)

        # now we jam up in the usual fashion for n_burn_in + n_imputations iterations
        results_list = []  # all of the imputed values, in a flattened format
        total_rounds = self.n_burn_in + self.n_imputations

        for m in range(total_rounds):
            if self.verbose:
                print(
                    "[MICE] Starting imputation round %d/%d, elapsed time %0.3f" % (
                        m + 1,
                        total_rounds,
                        time() - start_t))
            X_filled = self.perform_imputation_round(
                X_filled=X_filled,
                missing_mask=missing_mask,
                observed_mask=observed_mask,
                visit_indices=visit_indices,
                round_index=m)
            if m >= self.n_burn_in:
                results_list.append(X_filled[missing_mask])
        return np.array(results_list), missing_mask

    def complete(self, X):
        if self.verbose:
            print("[MICE] Completing matrix with shape %s" % (X.shape,))
        self.fitted_models = np.array(
            [[self.model for _ in range(X.shape[1])] for _ in range(self.n_burn_in + self.n_imputations)]
        )
        X_completed = np.array(X.copy())
        imputed_arrays, missing_mask = self.multiple_imputations(X)
        # average the imputed values for each feature
        average_imputated_values = imputed_arrays.mean(axis=0)
        X_completed[missing_mask] = average_imputated_values
        return X_completed

    def pickle_model(self, filename=None):
        """Export fitted models to a pickle file."""
        if self.fitted_models is None:
            print("No fitted models to save!")
            return

        if filename is None:
            filename = "fitted_models.pkl"

        # Save instance parameters
        with open(filename, 'w') as fp:
            pickle.dump(self.__dict__, fp)

    def complete_row(self, X, fitted_models=None, seed=None):
        """
        Complete missing values in a row using pre-fitted models.

        Note X and fitted models must be of type numpy array!
        """
        if fitted_models is None:
            fitted_models = self.fitted_models

        # Check if fitted_models is set or any values are missing
        if fitted_models is None:
            raise Exception("No saved fitted models!")

        # Check dimensions, skip if not row-like or wrong number of columns
        if X.ndim != 1:
            raise Exception("Input array is not row-like!")
        elif X.shape[0] != self.fitted_models.shape[1]:
            raise Exception("Input array has incorrect length!")

        # TODO compare number of columns to n_nearest_columns (should be <=)
        # This will affect the definition of other_column_indices

        # Check impute type
        if self.impute_type != 'col':
            raise Exception("Rows can only be completed if impute_type is 'col'!")

        # Check for missing values
        X_filled = X.copy()
        missing_mask = np.isnan(X_filled)
        n_missing = missing_mask.sum(axis=0)
        if n_missing == 0:
            print("The input row contains no missing data!")
            return X_filled

        n_cols = X_filled.shape[0]
        ordered_column_indices = np.arange(n_cols)

        # Check if visit indices are saved
        if self.visit_indices is None:
            raise Exception("No saved visit indices!")

        # Check if column init values are saved
        if self.column_init_values is None:
            raise Exception("No saved initial column values!")

        # Initialize missing values
        for col_idx in self.visit_indices:
            if missing_mask[col_idx]:
                X_filled[col_idx] = self.column_init_values[col_idx]

        # Seeding ensures reproducibility
        if seed is None:
            seed = self.seed
        if seed is not None:
            np.random.seed(seed)
        else:
            print("Random seed not set, results will not be reproducible!")

        results_list = []

        # Loop over imputation rounds
        for m in range(self.n_burn_in + self.n_imputations):
            # Loop over columns
            for col_idx in self.visit_indices:
                if missing_mask[col_idx]:
                    # Which columns were initially present?
                    # Case when using all other columns for imputation
                    other_column_indices = np.concatenate([
                        ordered_column_indices[:col_idx],
                        ordered_column_indices[col_idx + 1:]
                    ])
                    X_other_cols = X_filled[other_column_indices]
                    # Load the model for this round and column
                    brr = fitted_models[m][col_idx]
                    X_other_cols_missing = np.array([X_other_cols])  # no other rows!
                    mus, sigmas_squared = brr.predict_dist(X_other_cols_missing)
                    sigmas = sigmas_squared
                    np.sqrt(sigmas_squared, out=sigmas)
                    mus = mus[0]
                    sigmas = sigmas[0]
                    # print("Sampling from a normal distribution with mu={} and sigma={}".format(mus, sigmas))
                    imputed_value = np.random.normal(mus, sigmas)
                    imputed_value = self.clip(imputed_value)
                    X_filled[col_idx] = imputed_value
            # Save the imputed values
            if m >= self.n_burn_in:
                results_list.append(X_filled[missing_mask])

        # return X_filled
        imputed_arrays = np.array(results_list)
        X_completed = np.array(X.copy())
        # average the imputed values for each feature
        average_imputed_values = imputed_arrays.mean(axis=0)
        X_completed[missing_mask] = average_imputed_values
        return X_completed
